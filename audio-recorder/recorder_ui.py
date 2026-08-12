"""
GUI для аудио-рекордера: микрофон + системный звук -> MP3
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import threading
import tkinter as tk
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from recorder import Recorder

SETTINGS_FILE = Path(__file__).parent / 'recorder_settings.json'
ICON_FILE = Path(__file__).parent / 'icon.ico'
APP_USER_MODEL_ID = 'AudioRecorder.RecorderUI.1'


def _set_windows_app_id():
    # Без этого Windows группирует окно под иконкой python.exe в панели задач,
    # ПОЛНОСТЬЮ ИГНОРИРУЯ iconbitmap()/iconphoto() — таскбар берёт иконку по
    # AppUserModelID процесса, а не по иконке окна. Обязательно вызывать до
    # создания первого окна (Tk()).
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

METER_W, METER_H = 320, 16   # размер Canvas-баров в пикселях (высота canvas уменьшена на 1px сверху и снизу)
METER_SEGMENTS, METER_GAP = 30, 2   # число LED-сегментов эквалайзера и зазор между ними (px)

INVALID_NAME_CHARS = set('<>:"/\\|?*')
RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL',
                   *(f'COM{i}' for i in range(1, 10)),
                   *(f'LPT{i}' for i in range(1, 10))}

TRANSCRIPT_FIXER_URL = 'http://127.0.0.1:5000'

DEFAULT_SETTINGS: dict = {
    'output_dir':              str(Path(__file__).parent / 'recordings'),
    'silence_rms':             500,
    'silence_duration':        0.9,
    'min_speech_enabled':      True,
    'min_speech_duration':     0.5,
    'min_record_minutes':      0.0,
    'idle_timeout_minutes':    0.0,
    'mp3_bitrate':             128,
    'output_format':           'mp3',
    'fragment_record_enabled': True,
    'full_output_dir':         '',
    'mic_device_index':        -1,
    'sys_device_index':        -1,
    'meter_max':               2000,
    'auto_mic_on_level':       True,
    'settings_panel_visible':  True,
}


def _load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text('utf-8'))}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def _save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')


class RecorderApp(tk.Tk):
    def __init__(self):
        _set_windows_app_id()  # обязательно до создания первого окна (см. комментарий выше)
        super().__init__()
        self.title('Аудио Рекордер')
        if ICON_FILE.is_file():
            try:
                self.iconbitmap(default=str(ICON_FILE))
            except tk.TclError:
                pass
        self.resizable(False, False)
        self.lift()
        self.attributes('-topmost', True)
        self.after(200, lambda: self.attributes('-topmost', False))
        self.focus_force()
        self._recorder: Recorder | None = None
        self._recording = False
        self._saved_files: list[str] = []
        self._btn_pulse_job: str | None = None
        self._btn_pulse_step = 0
        self._mic_device_map: dict[str, int | None] = {}
        self._sys_device_map: dict[str, int | None] = {}
        self._record_start_time: float | None = None
        self._timer_job: str | None = None
        self._settings = _load_settings()
        self._settings_visible = self._settings.get('settings_panel_visible', True)
        self._build_ui()
        self._load_into_ui()
        self._scan_devices()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # pady у root не умеет в асимметричный (top, bottom) — задаём отступы
        # сверху/снизу явно на первом и последнем виджетах грида вместо этого.
        self.configure(padx=8)

        # Компактная тема: уменьшенный базовый шрифт для всех ttk-виджетов
        # (Label/Button/Checkbutton/Combobox/Entry/Spinbox/LabelFrame-заголовок).
        style = ttk.Style(self)
        style.configure('.', font=('Segoe UI', 8))

        # ── Settings frame (сворачивается кнопкой-шестерёнкой в строке управления) ─
        frm = ttk.LabelFrame(self, padding=(8, 4))
        frm.grid(row=0, column=0, sticky='ew', pady=(6, 4))
        self._frm_settings = frm

        lbl_kw  = {'sticky': 'w', 'pady': 2}
        wdg_kw  = {'sticky': 'w', 'padx': (6, 0), 'pady': 2}
        hint_fg = '#888888'

        # Папка для записи (основной непрерывный файл full_*, всегда включена)
        ttk.Label(frm, text='Папка для записи:').grid(row=0, column=0, **lbl_kw)
        frm_fdir = ttk.Frame(frm)
        frm_fdir.grid(row=0, column=1, **wdg_kw)
        self._var_full_dir = tk.StringVar()
        self._entry_full_dir = ttk.Entry(frm_fdir, textvariable=self._var_full_dir, width=30)
        self._entry_full_dir.pack(side='left')
        self._btn_full_browse = ttk.Button(frm_fdir, text='Обзор',
                                            command=self._browse_full)
        self._btn_full_browse.pack(side='left', padx=(6, 0))
        self._btn_full_open = ttk.Button(frm_fdir, text='📂 Открыть',
                                          command=self._open_full_dir)
        self._btn_full_open.pack(side='left', padx=(4, 0))

        # Порог тишины
        ttk.Label(frm, text='Порог тишины (RMS):').grid(row=1, column=0, **lbl_kw)
        frm_rms = ttk.Frame(frm)
        frm_rms.grid(row=1, column=1, **wdg_kw)
        self._var_rms = tk.IntVar()
        self._var_rms.trace_add('write', self._on_rms_changed)
        ttk.Spinbox(frm_rms, from_=50, to=5000, increment=50,
                    textvariable=self._var_rms, width=7).pack(side='left')
        ttk.Label(frm_rms, text='  (50–5000 · выше = менее чувствительный к шуму)',
                  foreground=hint_fg).pack(side='left')

        # Время отключения при тишине
        ttk.Label(frm, text='Время откл. при тишине:').grid(row=2, column=0, **lbl_kw)
        frm_it = ttk.Frame(frm)
        frm_it.grid(row=2, column=1, **wdg_kw)
        self._var_idle_timeout = tk.DoubleVar()
        ttk.Spinbox(frm_it, from_=0.0, to=120.0, increment=1.0, format='%.0f',
                    textvariable=self._var_idle_timeout, width=7).pack(side='left')
        ttk.Label(frm_it, text='  мин (0 = выкл) — полная остановка если нет звуков N минут',
                  foreground=hint_fg).pack(side='left')

        # Битрейт MP3 (всегда активен — нужен и для полной записи)
        ttk.Label(frm, text='Битрейт MP3:').grid(row=3, column=0, **lbl_kw)
        frm_br = ttk.Frame(frm)
        frm_br.grid(row=3, column=1, **wdg_kw)
        self._var_bitrate = tk.IntVar()
        self._cb_bitrate = ttk.Combobox(frm_br, textvariable=self._var_bitrate,
                                         values=[64, 96, 128, 192, 320],
                                         width=6, state='readonly')
        self._cb_bitrate.pack(side='left')
        ttk.Label(frm_br, text='  кбит/с', foreground=hint_fg).pack(side='left')

        # Запись фрагментов
        ttk.Label(frm, text='Запись фрагментов:').grid(row=4, column=0, **lbl_kw)
        frm_frag = ttk.Frame(frm)
        frm_frag.grid(row=4, column=1, **wdg_kw)
        self._var_fragment_record = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_frag, variable=self._var_fragment_record,
                        command=self._on_fragment_record_toggled).pack(side='left')
        ttk.Label(frm_frag,
                  text='  сохранять запись кусками по паузам речи (rec_*)',
                  foreground=hint_fg).pack(side='left')

        # ── Ниже — настройки, отключаемые чекбоксом "Запись фрагментов" ──────

        # Папка для фрагментов
        ttk.Label(frm, text='Папка для фрагментов:').grid(row=5, column=0, **lbl_kw)
        frm_dir = ttk.Frame(frm)
        frm_dir.grid(row=5, column=1, **wdg_kw)
        self._var_dir = tk.StringVar()
        self._entry_dir = ttk.Entry(frm_dir, textvariable=self._var_dir, width=30)
        self._entry_dir.pack(side='left')
        self._btn_dir_browse = ttk.Button(frm_dir, text='Обзор', command=self._browse)
        self._btn_dir_browse.pack(side='left', padx=(6, 0))
        self._btn_dir_open = ttk.Button(frm_dir, text='📂 Открыть',
                                         command=self._open_folder)
        self._btn_dir_open.pack(side='left', padx=(4, 0))

        # Длительность паузы
        ttk.Label(frm, text='Длительность паузы:').grid(row=6, column=0, **lbl_kw)
        frm_sd = ttk.Frame(frm)
        frm_sd.grid(row=6, column=1, **wdg_kw)
        self._var_silence_dur = tk.DoubleVar()
        self._spbx_silence_dur = ttk.Spinbox(frm_sd, from_=0.3, to=10.0, increment=0.1,
                                              format='%.1f', textvariable=self._var_silence_dur,
                                              width=7)
        self._spbx_silence_dur.pack(side='left')
        ttk.Label(frm_sd, text='  сек — пауза для завершения и сохранения файла',
                  foreground=hint_fg).pack(side='left')

        # Мин. длит. речи + чекбокс
        ttk.Label(frm, text='Мин. длит. речи:').grid(row=7, column=0, **lbl_kw)
        frm_ms = ttk.Frame(frm)
        frm_ms.grid(row=7, column=1, **wdg_kw)
        self._var_min_speech_on = tk.BooleanVar(value=True)
        self._var_min_speech = tk.DoubleVar()
        self._chk_min_speech_on = ttk.Checkbutton(frm_ms, variable=self._var_min_speech_on,
                                                   command=self._toggle_min_speech)
        self._chk_min_speech_on.pack(side='left')
        self._spbx_min_speech = ttk.Spinbox(frm_ms, from_=0.1, to=10.0, increment=0.1,
                                             format='%.1f', textvariable=self._var_min_speech,
                                             width=7)
        self._spbx_min_speech.pack(side='left', padx=(4, 0))
        ttk.Label(frm_ms, text='  сек — файлы короче не сохранять',
                  foreground=hint_fg).pack(side='left')

        # Мин. время записи
        ttk.Label(frm, text='Мин. время записи:').grid(row=8, column=0, **lbl_kw)
        frm_mr = ttk.Frame(frm)
        frm_mr.grid(row=8, column=1, **wdg_kw)
        self._var_min_record = tk.DoubleVar()
        self._spbx_min_record = ttk.Spinbox(frm_mr, from_=0.0, to=120.0, increment=0.5,
                                             format='%.1f', textvariable=self._var_min_record,
                                             width=7)
        self._spbx_min_record.pack(side='left')
        ttk.Label(frm_mr, text='  мин (0 = выкл) — не реагировать на тишину N минут',
                  foreground=hint_fg).pack(side='left')

        # Формат файла
        ttk.Label(frm, text='Формат файла:').grid(row=9, column=0, **lbl_kw)
        frm_fmt = ttk.Frame(frm)
        frm_fmt.grid(row=9, column=1, **wdg_kw)
        self._var_format = tk.StringVar()
        self._cb_format = ttk.Combobox(frm_fmt, textvariable=self._var_format,
                                        values=['mp3', 'wav'], width=6, state='readonly')
        self._cb_format.pack(side='left')
        ttk.Label(frm_fmt,
                  text='    WAV = без сжатия  |  MP3 = меньше размер',
                  foreground=hint_fg).pack(side='left')

        # Разделитель
        ttk.Separator(frm, orient='horizontal').grid(
            row=10, column=0, columnspan=2, sticky='ew', pady=(6, 2))

        # Микрофон — выбор устройства
        ttk.Label(frm, text='Микрофон:').grid(row=11, column=0, **lbl_kw)
        frm_mic_dev = ttk.Frame(frm)
        frm_mic_dev.grid(row=11, column=1, **wdg_kw)
        self._var_mic_device = tk.StringVar()
        self._cb_mic = ttk.Combobox(frm_mic_dev, textvariable=self._var_mic_device,
                                     width=40, state='readonly')
        self._cb_mic.pack(side='left')
        ttk.Button(frm_mic_dev, text='↻', width=3,
                   command=self._scan_devices).pack(side='left', padx=(6, 0))

        # Системный звук — выбор устройства
        ttk.Label(frm, text='Системный звук:').grid(row=12, column=0, **lbl_kw)
        frm_sys_dev = ttk.Frame(frm)
        frm_sys_dev.grid(row=12, column=1, **wdg_kw)
        self._var_sys_device = tk.StringVar()
        self._cb_sys = ttk.Combobox(frm_sys_dev, textvariable=self._var_sys_device,
                                     width=40, state='readonly')
        self._cb_sys.pack(side='left')

        # ── Controls ──────────────────────────────────────────────────────────
        frm_ctrl = ttk.Frame(self)
        frm_ctrl.grid(row=1, column=0, sticky='ew', pady=(0, 4))

        # Составная кнопка записи: Frame + Canvas-кружок (цветной, отдельно от текста —
        # обычная ttk/tk кнопка не умеет красить часть текста) + Label. Клик ловится
        # на всех трёх, т.к. они визуально образуют одну кнопку.
        self._btn = tk.Frame(frm_ctrl, bg='#e0e0e0', bd=1, relief='raised', cursor='hand2')
        self._btn.pack(side='left', ipadx=4, ipady=2)
        self._btn_dot = tk.Canvas(self._btn, width=16, height=16,
                                   highlightthickness=0, bg='#e0e0e0')
        self._btn_dot.pack(side='left', padx=(6, 4))
        self._btn_dot_id = self._btn_dot.create_oval(3, 3, 13, 13, fill='#cc3333', outline='')
        self._btn_label = tk.Label(self._btn, text='Начать запись',
                                    bg='#e0e0e0', font=('Segoe UI', 8))
        self._btn_label.pack(side='left', padx=(0, 6))
        self._btn_enabled = True
        for w in (self._btn, self._btn_dot, self._btn_label):
            w.bind('<Button-1>', lambda e: self._toggle() if self._btn_enabled else None)
        self._var_auto_mic = tk.BooleanVar()
        self._btn_mic_mute = tk.Button(frm_ctrl, text='🎤 Микрофон',
                                        command=self._toggle_mic_mute, state='disabled',
                                        bg='#e0e0e0', activebackground='#d0d0d0',
                                        relief='raised', bd=2, font=('Segoe UI', 8))
        self._btn_mic_mute.pack(side='left', padx=(6, 0), ipadx=4, ipady=1)
        self._chk_auto_mic = tk.Checkbutton(frm_ctrl, text='Авто-вкл. по уровню',
                                             variable=self._var_auto_mic,
                                             state='disabled',
                                             font=('Segoe UI', 8),
                                             fg='#555555', selectcolor='#e8ffe8',
                                             command=self._on_auto_mic_toggled)
        self._chk_auto_mic.pack(side='left', padx=(6, 0))
        self._btn_settings_toggle = tk.Canvas(frm_ctrl, width=18, height=18,
                                               highlightthickness=0, bd=1, relief='raised',
                                               bg=self.cget('bg'), cursor='hand2')
        self._btn_settings_toggle.pack(side='right')
        self._btn_settings_toggle.bind('<Button-1>', lambda e: self._toggle_settings_panel())
        self._btn_settings_toggle.bind(
            '<Enter>', lambda e: self._show_tooltip(self._btn_settings_toggle, 'Настройки'))
        self._btn_settings_toggle.bind('<Leave>', self._hide_tooltip)
        self._draw_dots_icon()

        # ── Meters frame ──────────────────────────────────────────────────────
        frm_m = ttk.LabelFrame(self, text='Уровни звука (RMS)', padding=(6, 3))
        frm_m.grid(row=2, column=0, sticky='ew', pady=(0, 4))

        # Микрофон
        ttk.Label(frm_m, text='Микрофон:', width=11, anchor='e').grid(
            row=0, column=0, padx=(0, 4), pady=1)
        self._canvas_mic = tk.Canvas(frm_m, width=METER_W, height=METER_H,
                                     bg='#1e1e1e', highlightthickness=1,
                                     highlightbackground='#555555')
        self._canvas_mic.grid(row=0, column=1, pady=1)
        self._lbl_mic = ttk.Label(frm_m, text='  —  ', width=6,
                                  anchor='e', font=('Consolas', 8))
        self._lbl_mic.grid(row=0, column=2, padx=(4, 0))

        # Динамики
        ttk.Label(frm_m, text='Динамики:', width=11, anchor='e').grid(
            row=1, column=0, padx=(0, 4), pady=1)
        self._canvas_sys = tk.Canvas(frm_m, width=METER_W, height=METER_H,
                                     bg='#1e1e1e', highlightthickness=1,
                                     highlightbackground='#555555')
        self._canvas_sys.grid(row=1, column=1, pady=1)
        self._lbl_sys = ttk.Label(frm_m, text='  —  ', width=6,
                                  anchor='e', font=('Consolas', 8))
        self._lbl_sys.grid(row=1, column=2, padx=(4, 0))

        # Верхний предел
        frm_max = ttk.Frame(frm_m)
        frm_max.grid(row=2, column=0, columnspan=3, sticky='w', pady=(2, 0))
        ttk.Label(frm_max, text='Верхний предел шкалы:').pack(side='left')
        self._var_meter_max = tk.IntVar()
        ttk.Spinbox(frm_max, from_=100, to=32768, increment=100,
                    textvariable=self._var_meter_max, width=7).pack(side='left', padx=(6, 0))
        ttk.Label(frm_max,
                  text='  белая черта = порог тишины',
                  foreground=hint_fg).pack(side='left')

        # ── Saved files list (скрыт во время записи, чтобы не занимать место) ──
        frm_files = ttk.LabelFrame(self, text='Записанные файлы', padding=4)
        frm_files.grid(row=4, column=0, sticky='nsew', pady=(0, 3))
        self.rowconfigure(4, weight=1)
        self.columnconfigure(0, weight=1)
        self._frm_files = frm_files

        self._listbox = tk.Listbox(frm_files, width=72, height=3,
                                   font=('Consolas', 8), activestyle='none')
        sb = ttk.Scrollbar(frm_files, orient='vertical', command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=sb.set)
        self._listbox.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self._listbox.bind('<Double-Button-1>', self._open_file)
        self._listbox.bind('<Button-3>', self._on_file_right_click)

        self._lbl_files_hint = ttk.Label(
            self, text='Двойной клик — открыть · правая кнопка — меню',
            foreground='#888888', font=('Segoe UI', 7), padding=(0, 0))
        self._lbl_files_hint.grid(row=5, column=0, sticky='w', pady=(0, 3))

        self._apply_settings_panel_visibility()
        self._set_files_panel_visible(False)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _load_into_ui(self):
        s = self._settings
        self._var_dir.set(s['output_dir'])
        self._var_rms.set(s['silence_rms'])
        self._var_silence_dur.set(s['silence_duration'])
        self._var_min_speech_on.set(s.get('min_speech_enabled', True))
        self._var_min_speech.set(s['min_speech_duration'])
        self._var_min_record.set(s.get('min_record_minutes', 0.0))
        self._var_idle_timeout.set(s.get('idle_timeout_minutes', 0.0))
        self._var_bitrate.set(s['mp3_bitrate'])
        self._var_format.set(s.get('output_format', 'mp3'))
        self._var_fragment_record.set(s.get('fragment_record_enabled', True))
        self._var_full_dir.set(s.get('full_output_dir', ''))
        # _on_fragment_record_toggled() ниже уже приводит спинбокс мин. длит. речи
        # в нужное состояние (через _toggle_min_speech при включённых фрагментах,
        # либо force-disabled при выключенных) — отдельный вызов не нужен.
        self._on_fragment_record_toggled()
        # Devices restored in _scan_devices() which is called after _load_into_ui()
        self._var_meter_max.set(s.get('meter_max', 2000))
        self._var_auto_mic.set(s.get('auto_mic_on_level', False))

    def _scan_devices(self):
        import pyaudiowpatch as pyaudio
        saved_mic = self._settings.get('mic_device_index', -1)
        saved_sys = self._settings.get('sys_device_index', -1)

        AUTO_MIC = 'Авто  (дефолтный микрофон)'
        AUTO_SYS = 'Авто  (дефолтный loopback)'

        mic_items = [AUTO_MIC]
        sys_items = [AUTO_SYS]
        self._mic_device_map = {AUTO_MIC: None}
        self._sys_device_map = {AUTO_SYS: None}

        try:
            pa = pyaudio.PyAudio()
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d['maxInputChannels'] > 0 and not d.get('isLoopbackDevice', False):
                    ch = int(d['maxInputChannels'])
                    hz = int(d['defaultSampleRate'])
                    lbl = f"[{i}]  {d['name'][:42]}  {ch}ch  {hz}Hz"
                    mic_items.append(lbl)
                    self._mic_device_map[lbl] = i
            for lb in pa.get_loopback_device_info_generator():
                ch = int(lb['maxInputChannels'])
                hz = int(lb['defaultSampleRate'])
                lbl = f"[{lb['index']}]  {lb['name'][:42]}  {ch}ch  {hz}Hz"
                sys_items.append(lbl)
                self._sys_device_map[lbl] = int(lb['index'])
            pa.terminate()
        except Exception:
            pass

        self._cb_mic['values'] = mic_items
        self._cb_sys['values'] = sys_items

        # Restore saved selection by index
        for lbl, idx in self._mic_device_map.items():
            if (saved_mic == -1 and idx is None) or idx == saved_mic:
                self._var_mic_device.set(lbl)
                break
        else:
            self._var_mic_device.set(AUTO_MIC)

        for lbl, idx in self._sys_device_map.items():
            if (saved_sys == -1 and idx is None) or idx == saved_sys:
                self._var_sys_device.set(lbl)
                break
        else:
            self._var_sys_device.set(AUTO_SYS)

    def _on_fragment_record_toggled(self):
        enabled = self._var_fragment_record.get()
        state = 'normal' if enabled else 'disabled'
        self._entry_dir.config(state=state)
        self._btn_dir_browse.config(state=state)
        self._btn_dir_open.config(state=state)
        self._spbx_silence_dur.config(state=state)
        self._chk_min_speech_on.config(state=state)
        self._spbx_min_record.config(state=state)
        self._cb_format.config(state='readonly' if enabled else 'disabled')
        if enabled:
            self._toggle_min_speech()  # вернуть спинбокс под управление его чекбокса
        else:
            self._spbx_min_speech.config(state='disabled')
        # Сохраняем сразу — иначе теряется, если пользователь не запустит
        # запись повторно перед закрытием приложения (та же причина, что и
        # для видимости панели настроек).
        self._settings['fragment_record_enabled'] = enabled
        _save_settings(self._settings)

    def _toggle_settings_panel(self):
        self._settings_visible = not self._settings_visible
        self._apply_settings_panel_visibility()
        self.update_idletasks()
        self.geometry('')
        # Сохраняем сразу, а не ждём следующего _start() — иначе состояние
        # теряется, если пользователь переключил панель и просто закрыл окно.
        self._settings['settings_panel_visible'] = self._settings_visible
        _save_settings(self._settings)

    def _apply_settings_panel_visibility(self):
        if self._settings_visible:
            self._frm_settings.grid()
            self._btn_settings_toggle.config(relief='sunken')
        else:
            self._frm_settings.grid_remove()
            self._btn_settings_toggle.config(relief='raised')

    def _draw_dots_icon(self, color: str = '#333333'):
        # Три точки вместо шестерёнки — компактнее и проще на маленькой кнопке.
        c = self._btn_settings_toggle
        c.delete('all')
        cy = 9
        r = 1.4
        for cx in (5, 9, 13):
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline='')

    def _show_tooltip(self, widget, text):
        if getattr(self, '_tooltip_win', None):
            return
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        tw = tk.Toplevel(widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f'+{x}+{y}')
        tk.Label(tw, text=text, background='#ffffe0', relief='solid', borderwidth=1,
                 font=('Segoe UI', 8), padx=4, pady=2).pack()
        self._tooltip_win = tw

    def _hide_tooltip(self, _=None):
        tw = getattr(self, '_tooltip_win', None)
        if tw:
            tw.destroy()
            self._tooltip_win = None

    def _set_files_panel_visible(self, visible: bool):
        if visible:
            self._frm_files.grid()
            self._lbl_files_hint.grid()
        else:
            self._frm_files.grid_remove()
            self._lbl_files_hint.grid_remove()
        self.update_idletasks()
        self.geometry('')

    def _browse_full(self):
        d = filedialog.askdirectory(initialdir=self._var_full_dir.get() or self._var_dir.get())
        if d:
            self._var_full_dir.set(d)

    def _open_full_dir(self):
        d = self._var_full_dir.get().strip() or self._var_dir.get().strip() or str(Path(__file__).parent / 'recordings')
        Path(d).mkdir(parents=True, exist_ok=True)
        os.startfile(d)

    def _on_rms_changed(self, *_):
        if self._recorder is not None:
            try:
                self._recorder._silence_rms = int(self._var_rms.get())
            except (ValueError, tk.TclError):
                pass

    def _toggle_min_speech(self):
        state = 'normal' if self._var_min_speech_on.get() else 'disabled'
        self._spbx_min_speech.config(state=state)

    def _collect(self) -> dict:
        return {
            'output_dir':              self._var_dir.get().strip(),
            'silence_rms':             int(self._var_rms.get()),
            'silence_duration':        float(self._var_silence_dur.get()),
            'min_speech_enabled':      self._var_min_speech_on.get(),
            'min_speech_duration':     float(self._var_min_speech.get()),
            'min_record_minutes':      float(self._var_min_record.get()),
            'idle_timeout_minutes':    float(self._var_idle_timeout.get()),
            'mp3_bitrate':             int(self._var_bitrate.get()),
            'output_format':           self._var_format.get(),
            'fragment_record_enabled': self._var_fragment_record.get(),
            'full_output_dir':         self._var_full_dir.get().strip(),
            'mic_device_index':        self._mic_device_map.get(self._var_mic_device.get(), -1) or -1,
            'sys_device_index':        self._sys_device_map.get(self._var_sys_device.get(), -1) or -1,
            'meter_max':               int(self._var_meter_max.get()),
            'auto_mic_on_level':       self._var_auto_mic.get(),
            'settings_panel_visible':  self._settings_visible,
        }

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._var_dir.get())
        if d:
            self._var_dir.set(d)

    def _open_folder(self):
        d = self._var_dir.get().strip() or str(Path(__file__).parent / 'recordings')
        Path(d).mkdir(parents=True, exist_ok=True)
        os.startfile(d)

    def _open_file(self, _=None):
        sel = self._listbox.curselection()
        if sel and sel[0] < len(self._saved_files):
            p = self._saved_files[sel[0]]
            if not os.path.isfile(p):
                return
            try:
                os.startfile(p)
            except OSError:
                try:
                    subprocess.Popen(['cmd', '/c', 'start', '', p],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
                except Exception:
                    pass

    def _on_file_right_click(self, event):
        if self._listbox.size() == 0:
            return
        idx = self._listbox.nearest(event.y)
        bbox = self._listbox.bbox(idx)
        if not bbox or not (bbox[1] <= event.y <= bbox[1] + bbox[3]):
            return
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(idx)
        self._listbox.activate(idx)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label='✏ Переименовать', command=self._rename_selected_file)
        menu.add_command(label='📂 Открыть папку', command=self._open_selected_file_folder)
        menu.add_separator()
        menu.add_command(label='📤 Отправить в Transcript Fixer',
                          command=self._send_to_transcript_fixer)
        menu.add_separator()
        menu.add_command(label='🗑 Удалить', command=self._delete_selected_file)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _ask_new_filename(self, initial: str) -> str | None:
        # Свой диалог вместо simpledialog.askstring — у стандартного почему-то
        # не срабатывает Ctrl+V на некоторых раскладках/системах (штатная
        # виртуальная привязка <<Paste>> молчит). Здесь вставка явно завязана
        # на буфер обмена (несколько сочетаний клавиш) + пункт в контекстном
        # меню по правому клику — так работает гарантированно, не полагаясь
        # на дефолтные Tk-бинды.
        dlg = tk.Toplevel(self)
        dlg.title('Переименовать')
        dlg.transient(self)
        dlg.resizable(False, False)

        ttk.Label(dlg, text='Новое имя файла (без расширения):').pack(
            padx=10, pady=(10, 4), anchor='w')
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(dlg, textvariable=var, width=42)
        entry.pack(padx=10, pady=(0, 8), fill='x')
        entry.select_range(0, 'end')
        entry.icursor('end')

        result: dict[str, str | None] = {'value': None}

        def _paste(_event=None):
            try:
                text = dlg.clipboard_get()
            except tk.TclError:
                return 'break'
            try:
                entry.delete('sel.first', 'sel.last')
            except tk.TclError:
                pass
            entry.insert('insert', text)
            return 'break'

        def _copy(_event=None):
            try:
                sel = entry.selection_get()
            except tk.TclError:
                return 'break'
            dlg.clipboard_clear()
            dlg.clipboard_append(sel)
            return 'break'

        def _cut(_event=None):
            _copy()
            try:
                entry.delete('sel.first', 'sel.last')
            except tk.TclError:
                pass
            return 'break'

        def _select_all(_event=None):
            entry.select_range(0, 'end')
            entry.icursor('end')
            return 'break'

        for seq in ('<Control-v>', '<Control-V>', '<Shift-Insert>'):
            entry.bind(seq, _paste)
        for seq in ('<Control-c>', '<Control-C>', '<Control-Insert>'):
            entry.bind(seq, _copy)
        for seq in ('<Control-x>', '<Control-X>'):
            entry.bind(seq, _cut)
        for seq in ('<Control-a>', '<Control-A>'):
            entry.bind(seq, _select_all)

        ctx_menu = tk.Menu(entry, tearoff=0)
        ctx_menu.add_command(label='Вырезать', command=_cut)
        ctx_menu.add_command(label='Копировать', command=_copy)
        ctx_menu.add_command(label='Вставить', command=_paste)
        ctx_menu.add_separator()
        ctx_menu.add_command(label='Выделить всё', command=_select_all)
        entry.bind('<Button-3>', lambda e: ctx_menu.tk_popup(e.x_root, e.y_root))

        def _ok(_event=None):
            result['value'] = var.get()
            dlg.destroy()

        def _cancel(_event=None):
            result['value'] = None
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(padx=10, pady=(0, 10), fill='x')
        ttk.Button(btns, text='Отмена', command=_cancel).pack(side='right')
        ttk.Button(btns, text='OK', command=_ok).pack(side='right', padx=(0, 6))

        entry.bind('<Return>', _ok)
        dlg.bind('<Escape>', _cancel)
        dlg.protocol('WM_DELETE_WINDOW', _cancel)

        entry.focus_set()
        dlg.grab_set()
        dlg.wait_window(dlg)
        return result['value']

    def _rename_selected_file(self):
        sel = self._listbox.curselection()
        if not sel or sel[0] >= len(self._saved_files):
            return
        idx = sel[0]
        old = Path(self._saved_files[idx])
        if not old.is_file():
            messagebox.showerror('Переименование', 'Файл не найден на диске.')
            return

        new_stem = self._ask_new_filename(old.stem)
        if new_stem is None:
            return
        new_stem = new_stem.strip()
        if not new_stem:
            messagebox.showerror('Переименование', 'Имя не может быть пустым.')
            return
        if set(new_stem) & INVALID_NAME_CHARS:
            messagebox.showerror('Переименование',
                                  'Имя содержит недопустимые символы: < > : " / \\ | ? *')
            return
        if new_stem.upper() in RESERVED_NAMES:
            messagebox.showerror('Переименование',
                                  f'"{new_stem}" — зарезервированное имя в Windows.')
            return

        new_path = old.with_name(new_stem + old.suffix)
        if new_path.name == old.name:
            return
        if new_path.exists() and new_path.name.lower() != old.name.lower():
            messagebox.showerror('Переименование', f'Файл "{new_path.name}" уже существует.')
            return

        try:
            old.rename(new_path)
        except OSError as e:
            messagebox.showerror('Переименование', f'Не удалось переименовать: {e}')
            return

        self._saved_files[idx] = str(new_path)
        rest = self._listbox.get(idx)[48:]   # длительность + размер — без изменений
        self._listbox.delete(idx)
        self._listbox.insert(idx, f'{new_path.name:<46}  {rest}')
        self._listbox.selection_set(idx)

    def _send_to_transcript_fixer(self):
        sel = self._listbox.curselection()
        if not sel or sel[0] >= len(self._saved_files):
            return
        path = self._saved_files[sel[0]]
        if not os.path.isfile(path):
            messagebox.showerror('Transcript Fixer', 'Файл не найден на диске.')
            return
        name = Path(path).name
        self._open_transcript_fixer(path, name)

    def _open_transcript_fixer(self, server_path: str = '', filename: str = ''):
        # Тот же способ открытия, что и в собственном start_app.bat у Transcript
        # Fixer (отдельное окно приложения через --app=, не обычная вкладка) —
        # иначе выглядит как "чужой" браузер/профиль. addfile в URL подхватывает
        # app.js и кладёт файл в ту же очередь распознавания, что и autoscan/
        # ручное добавление (без включения самого режима autoscan), поэтому
        # там же честно применяются все настройки — авто-анализ, авто-замена,
        # авто-саммари.
        url = TRANSCRIPT_FIXER_URL
        if server_path:
            st = os.stat(server_path)
            params = {
                'addfile': server_path,
                'filename': filename,
                'size': str(st.st_size),
                'created': str(int(st.st_mtime)),
            }
            url += '/?' + urllib.parse.urlencode(params)
        candidates = [
            os.environ.get('ProgramFiles', '') + r'\Google\Chrome\Application\chrome.exe',
            os.environ.get('ProgramFiles(x86)', '') + r'\Google\Chrome\Application\chrome.exe',
            os.environ.get('LocalAppData', '') + r'\Google\Chrome\Application\chrome.exe',
            os.environ.get('ProgramFiles(x86)', '') + r'\Microsoft\Edge\Application\msedge.exe',
            os.environ.get('ProgramFiles', '') + r'\Microsoft\Edge\Application\msedge.exe',
        ]
        for exe in candidates:
            if os.path.isfile(exe):
                subprocess.Popen([exe, f'--app={url}'])
                return
        webbrowser.open(url)

    def _delete_selected_file(self):
        sel = self._listbox.curselection()
        if not sel or sel[0] >= len(self._saved_files):
            return
        idx = sel[0]
        path = self._saved_files[idx]
        if not os.path.isfile(path):
            messagebox.showerror('Удаление', 'Файл не найден на диске.')
            return
        if not messagebox.askokcancel('Удалить файл', f'Удалить файл?\n\n{path}'):
            return
        try:
            os.remove(path)
        except OSError as e:
            messagebox.showerror('Удаление', f'Не удалось удалить: {e}')
            return
        del self._saved_files[idx]
        self._listbox.delete(idx)

    def _open_selected_file_folder(self):
        sel = self._listbox.curselection()
        if not sel or sel[0] >= len(self._saved_files):
            return
        path = self._saved_files[sel[0]]
        if not os.path.isfile(path):
            messagebox.showerror('Открыть папку', 'Файл не найден на диске.')
            return
        subprocess.Popen(['explorer', f'/select,{path}'])

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle(self):
        if not self._recording:
            self._start()
        else:
            self._stop()

    def _set_record_btn(self, text: str, recording: bool = False, enabled: bool = True) -> None:
        # Форма значка повторяет прежние текстовые ●/■: кружок — можно начать
        # запись, квадрат — запись идёт (плавно пульсирует, пока не отключена).
        self._btn_enabled = enabled
        self._stop_btn_pulse()
        if not enabled:
            bg, dot_color = '#d5d5d5', '#aaaaaa'
        else:
            bg, dot_color = '#e0e0e0', '#cc3333'
        self._btn.config(bg=bg)
        self._btn_dot.config(bg=bg)
        self._btn_dot.delete('all')
        if recording:
            self._btn_dot_id = self._btn_dot.create_rectangle(3, 3, 13, 13,
                                                                fill=dot_color, outline='')
        else:
            self._btn_dot_id = self._btn_dot.create_oval(3, 3, 13, 13,
                                                          fill=dot_color, outline='')
        self._btn_label.config(text=text, bg=bg)
        if recording and enabled:
            self._start_btn_pulse()

    @staticmethod
    def _lerp_color(c1: str, c2: str, t: float) -> str:
        t = max(0.0, min(1.0, t))
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = round(r1 + (r2 - r1) * t)
        g = round(g1 + (g2 - g1) * t)
        b = round(b1 + (b2 - b1) * t)
        return f'#{r:02x}{g:02x}{b:02x}'

    def _start_btn_pulse(self):
        self._btn_pulse_step = 0
        self._tick_btn_pulse()

    def _tick_btn_pulse(self):
        if not self._recording or not self._btn_enabled:
            self._btn_pulse_job = None
            return
        step = self._btn_pulse_step
        # Треугольная волна 0→1→0 за 40 тиков по 40мс (~1.6с цикл) — плавное
        # затемнение/осветление вместо резкого переключения цвета.
        t = step / 20 if step <= 20 else (40 - step) / 20
        self._btn_dot.itemconfig(self._btn_dot_id,
                                 fill=self._lerp_color('#661111', '#ff4444', t))
        self._btn_pulse_step = (step + 1) % 40
        self._btn_pulse_job = self.after(40, self._tick_btn_pulse)

    def _stop_btn_pulse(self):
        if self._btn_pulse_job:
            self.after_cancel(self._btn_pulse_job)
            self._btn_pulse_job = None

    def _update_mic_btn(self, muted: bool) -> None:
        if muted:
            self._btn_mic_mute.config(
                text='🔇 Микрофон', relief='sunken',
                bg='#f0b0b0', activebackground='#e8a0a0')
        else:
            self._btn_mic_mute.config(
                text='🎤 Микрофон', relief='raised',
                bg='#e0e0e0', activebackground='#d0d0d0')

    def _toggle_mic_mute(self):
        if not self._recorder:
            return
        self._recorder._mic_muted = not self._recorder._mic_muted
        self._update_mic_btn(self._recorder._mic_muted)

    def _on_auto_mic_toggled(self):
        # Чекбокс доступен только во время записи, а сохранение раньше
        # происходило лишь при _start() — если не запустить запись заново
        # перед закрытием, значение терялось. Сохраняем сразу, как и с
        # "Запись фрагментов" / видимостью панели настроек.
        self._settings['auto_mic_on_level'] = self._var_auto_mic.get()
        _save_settings(self._settings)

    def _start(self):
        s = self._collect()
        _save_settings(s)
        self._settings = s
        self._reset_meters()
        self._set_files_panel_visible(False)

        min_speech_dur = s['min_speech_duration'] if s['min_speech_enabled'] else 0.0

        try:
            self._recorder = Recorder(
                silence_rms=s['silence_rms'],
                silence_duration=s['silence_duration'],
                min_speech_duration=min_speech_dur,
                mp3_bitrate=s['mp3_bitrate'],
                output_dir=s['output_dir'],
                min_record_secs=s['min_record_minutes'] * 60.0,
                idle_timeout_secs=s['idle_timeout_minutes'] * 60.0,
                output_format=s['output_format'],
                full_record=True,
                full_output_dir=s['full_output_dir'],
                fragment_record=s['fragment_record_enabled'],
                mic_device=None if s['mic_device_index'] == -1 else s['mic_device_index'],
                sys_device=None if s['sys_device_index'] == -1 else s['sys_device_index'],
                on_file_saved=self._cb_file,
                on_idle_timeout=self._cb_idle_timeout,
                on_levels=self._cb_levels,
            )
            self._recorder.start()
        except Exception as e:
            messagebox.showerror('Ошибка запуска', str(e))
            return

        self._recording = True
        self._set_record_btn('Остановить  (0:00)', recording=True, enabled=True)
        # Старт с замьюченным микрофоном если включён авто-вкл
        start_muted = self._var_auto_mic.get()
        self._recorder._mic_muted = start_muted
        self._update_mic_btn(start_muted)
        self._btn_mic_mute.config(state='normal')
        self._chk_auto_mic.config(state='normal')
        self._start_timer()

    def _stop(self):
        self._recording = False
        self._set_record_btn(self._btn_label['text'], recording=True, enabled=False)

        def _do():
            try:
                files = self._recorder.stop() if self._recorder else []
            except Exception as e:
                files = []
                # Python удаляет переменную `except ... as e` сразу после блока —
                # если захватить её лямбдой напрямую, self.after() вызовет лямбду
                # позже и получит NameError вместо сообщения об ошибке.
                err_msg = str(e)
                self.after(0, lambda: messagebox.showerror(
                    'Ошибка остановки', err_msg))
            self.after(0, self._on_stopped, files)

        threading.Thread(target=_do, daemon=True).start()

    def _on_stopped(self, files: list[str]):
        self._finish_recording()

    # ── Callbacks from recorder thread ────────────────────────────────────────

    def _cb_file(self, path: str, duration_sec: float):
        def _upd():
            self._saved_files.append(path)
            mins = int(duration_sec // 60)
            secs = int(duration_sec % 60)
            dur_str = f"{mins}:{secs:02d}"
            try:
                size_mb = Path(path).stat().st_size / (1024 * 1024)
                size_str = f"{size_mb:.1f} MB"
            except Exception:
                size_str = ''
            name = Path(path).name
            entry = f"{name:<46}  {dur_str:>5}   {size_str:>7}"
            self._listbox.insert(tk.END, entry)
            self._listbox.see(tk.END)
        self.after(0, _upd)

    def _cb_levels(self, mic_rms: float, sys_rms: float):
        def _upd():
            if not self._recorder:
                return
            muted = self._recorder._mic_muted
            self._draw_meter(self._canvas_mic, self._lbl_mic, mic_rms, muted=muted)
            self._draw_meter(self._canvas_sys, self._lbl_sys, sys_rms)
            if (muted and self._var_auto_mic.get()
                    and mic_rms > self._recorder._silence_rms):
                self._recorder._mic_muted = False
                self._update_mic_btn(False)
        self.after(0, _upd)

    def _cb_idle_timeout(self):
        self.after(0, self._handle_idle_stop)

    def _handle_idle_stop(self):
        self._recording = False
        self._set_record_btn(self._btn_label['text'], recording=True, enabled=False)

        def _do():
            if self._recorder:
                self._recorder.stop()
            self.after(0, self._on_idle_done)

        threading.Thread(target=_do, daemon=True).start()

    def _on_idle_done(self):
        self._finish_recording()

    def _finish_recording(self):
        self._recorder = None
        self._set_record_btn('Начать запись', recording=False, enabled=True)
        self._update_mic_btn(False)
        self._btn_mic_mute.config(state='disabled')
        self._chk_auto_mic.config(state='disabled')
        self._reset_meters()
        self._stop_timer()
        self._set_files_panel_visible(True)

    # ── Meters ────────────────────────────────────────────────────────────────

    @staticmethod
    def _segment_color(index: int, total: int) -> str:
        # Цвет как в эквалайзерах музыкальных центров: зелёные сегменты внизу
        # шкалы, жёлтые ближе к верху, красные на пике — по позиции сегмента,
        # а не по текущему порогу тишины (тот показан отдельно белой чертой).
        frac = index / max(1, total - 1)
        if frac < 0.6:
            return '#33aa55'
        if frac < 0.85:
            return '#cc9900'
        return '#cc3333'

    def _draw_meter(self, canvas: tk.Canvas, label: ttk.Label, rms: float, muted: bool = False):
        threshold = float(max(1, self._var_rms.get()))
        max_rms   = float(max(1, self._var_meter_max.get()))
        # Уровень отображается всегда, даже при выключенном микрофоне (как и
        # раньше со сплошной заливкой) — просто закрашенные сегменты серые,
        # а не по обычной зелёной/жёлтой/красной шкале.
        filled = int(round(METER_SEGMENTS * min(1.0, rms / max_rms)))

        seg_total_w = METER_W / METER_SEGMENTS
        seg_w = max(1.0, seg_total_w - METER_GAP)

        canvas.delete('all')
        for i in range(METER_SEGMENTS):
            x0 = i * seg_total_w
            if i >= filled:
                color = '#333333'
            else:
                color = '#777777' if muted else self._segment_color(i, METER_SEGMENTS)
            canvas.create_rectangle(x0, 0, x0 + seg_w, METER_H, fill=color, outline='')

        # Маркер порога тишины
        thr_x = METER_W * min(1.0, threshold / max_rms)
        if 0 < thr_x <= METER_W:
            canvas.create_line(thr_x, 0, thr_x, METER_H, fill='white', width=1)

        label.config(text='ОТКЛ' if muted else f'{int(rms):>5}',
                     foreground='#cc2222' if muted else '')

    def _reset_meters(self):
        for canvas, label in ((self._canvas_mic, self._lbl_mic),
                               (self._canvas_sys, self._lbl_sys)):
            canvas.delete('all')
            label.config(text='  —  ')

    # ── Timers / status / blink ──────────────────────────────────────────────

    def _start_timer(self):
        self._record_start_time = time.monotonic()
        self._tick_timer()

    def _tick_timer(self):
        if not self._recording:
            return
        total = time.monotonic() - self._record_start_time
        tm, ts = divmod(int(total), 60)
        # Общее время записи показывается прямо в тексте кнопки «Остановить».
        self._btn_label.config(text=f'Остановить  ({tm}:{ts:02d})')
        self._timer_job = self.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_job:
            self.after_cancel(self._timer_job)
            self._timer_job = None
        self._record_start_time = None

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._recording and self._recorder:
            self._recorder.stop()
        self.destroy()


if __name__ == '__main__':
    app = RecorderApp()
    app.mainloop()
