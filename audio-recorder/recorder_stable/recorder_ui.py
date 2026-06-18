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
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from recorder import Recorder

SETTINGS_FILE = Path(__file__).parent / 'recorder_settings.json'

METER_W, METER_H = 320, 20   # размер Canvas-баров в пикселях

DEFAULT_SETTINGS: dict = {
    'output_dir':           str(Path(__file__).parent / 'recordings'),
    'silence_rms':          500,
    'silence_duration':     0.9,
    'min_speech_enabled':   True,
    'min_speech_duration':  0.5,
    'min_record_minutes':   0.0,
    'idle_timeout_minutes': 0.0,
    'mp3_bitrate':          128,
    'output_format':        'mp3',
    'full_record_enabled':  False,
    'full_output_dir':      '',
    'mic_device_index':     -1,
    'sys_device_index':     -1,
    'meter_max':            2000,
    'auto_mic_on_level':    False,
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
        super().__init__()
        self.title('Аудио Рекордер')
        self.resizable(False, False)
        self.lift()
        self.attributes('-topmost', True)
        self.after(200, lambda: self.attributes('-topmost', False))
        self.focus_force()
        self._recorder: Recorder | None = None
        self._recording = False
        self._saved_files: list[str] = []
        self._blink_job: str | None = None
        self._blink_state = False
        self._blink_color = 'gray'
        self._mic_device_map: dict[str, int | None] = {}
        self._sys_device_map: dict[str, int | None] = {}
        self._record_start_time: float | None = None
        self._file_start_time:   float | None = None
        self._timer_job: str | None = None
        self._settings = _load_settings()
        self._build_ui()
        self._load_into_ui()
        self._scan_devices()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.configure(padx=16, pady=14)

        # ── Settings frame ────────────────────────────────────────────────────
        frm = ttk.LabelFrame(self, text='Настройки', padding=(12, 8))
        frm.grid(row=0, column=0, sticky='ew', pady=(0, 10))

        lbl_kw  = {'sticky': 'w', 'pady': 5}
        wdg_kw  = {'sticky': 'w', 'padx': (10, 0), 'pady': 5}
        hint_fg = '#888888'

        # Папка сохранения
        ttk.Label(frm, text='Папка сохранения:').grid(row=0, column=0, **lbl_kw)
        frm_dir = ttk.Frame(frm)
        frm_dir.grid(row=0, column=1, **wdg_kw)
        self._var_dir = tk.StringVar()
        ttk.Entry(frm_dir, textvariable=self._var_dir, width=38).pack(side='left')
        ttk.Button(frm_dir, text='Обзор', command=self._browse).pack(side='left', padx=(6, 0))

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

        # Длительность паузы
        ttk.Label(frm, text='Длительность паузы:').grid(row=2, column=0, **lbl_kw)
        frm_sd = ttk.Frame(frm)
        frm_sd.grid(row=2, column=1, **wdg_kw)
        self._var_silence_dur = tk.DoubleVar()
        ttk.Spinbox(frm_sd, from_=0.3, to=10.0, increment=0.1, format='%.1f',
                    textvariable=self._var_silence_dur, width=7).pack(side='left')
        ttk.Label(frm_sd, text='  сек — пауза для завершения и сохранения файла',
                  foreground=hint_fg).pack(side='left')

        # Мин. длит. речи + чекбокс
        ttk.Label(frm, text='Мин. длит. речи:').grid(row=3, column=0, **lbl_kw)
        frm_ms = ttk.Frame(frm)
        frm_ms.grid(row=3, column=1, **wdg_kw)
        self._var_min_speech_on = tk.BooleanVar(value=True)
        self._var_min_speech = tk.DoubleVar()
        ttk.Checkbutton(frm_ms, variable=self._var_min_speech_on,
                        command=self._toggle_min_speech).pack(side='left')
        self._spbx_min_speech = ttk.Spinbox(frm_ms, from_=0.1, to=10.0, increment=0.1,
                                             format='%.1f', textvariable=self._var_min_speech,
                                             width=7)
        self._spbx_min_speech.pack(side='left', padx=(4, 0))
        ttk.Label(frm_ms, text='  сек — файлы короче не сохранять',
                  foreground=hint_fg).pack(side='left')

        # Мин. время записи
        ttk.Label(frm, text='Мин. время записи:').grid(row=4, column=0, **lbl_kw)
        frm_mr = ttk.Frame(frm)
        frm_mr.grid(row=4, column=1, **wdg_kw)
        self._var_min_record = tk.DoubleVar()
        ttk.Spinbox(frm_mr, from_=0.0, to=120.0, increment=0.5, format='%.1f',
                    textvariable=self._var_min_record, width=7).pack(side='left')
        ttk.Label(frm_mr, text='  мин (0 = выкл) — не реагировать на тишину N минут с начала файла',
                  foreground=hint_fg).pack(side='left')

        # Время отключения при тишине
        ttk.Label(frm, text='Время откл. при тишине:').grid(row=5, column=0, **lbl_kw)
        frm_it = ttk.Frame(frm)
        frm_it.grid(row=5, column=1, **wdg_kw)
        self._var_idle_timeout = tk.DoubleVar()
        ttk.Spinbox(frm_it, from_=0.0, to=120.0, increment=1.0, format='%.0f',
                    textvariable=self._var_idle_timeout, width=7).pack(side='left')
        ttk.Label(frm_it, text='  мин (0 = выкл) — полная остановка если нет звуков N минут',
                  foreground=hint_fg).pack(side='left')

        # Формат файла
        ttk.Label(frm, text='Формат файла:').grid(row=6, column=0, **lbl_kw)
        frm_fmt = ttk.Frame(frm)
        frm_fmt.grid(row=6, column=1, **wdg_kw)
        self._var_format = tk.StringVar()
        ttk.Combobox(frm_fmt, textvariable=self._var_format,
                     values=['mp3', 'wav'], width=6, state='readonly',
                     ).pack(side='left')
        self._var_format.trace_add('write', self._on_format_changed)
        ttk.Label(frm_fmt,
                  text='    WAV = без сжатия, лучше для Whisper  |  MP3 = меньше размер',
                  foreground=hint_fg).pack(side='left')

        # Битрейт MP3
        ttk.Label(frm, text='Битрейт MP3:').grid(row=7, column=0, **lbl_kw)
        frm_br = ttk.Frame(frm)
        frm_br.grid(row=7, column=1, **wdg_kw)
        self._var_bitrate = tk.IntVar()
        self._cb_bitrate = ttk.Combobox(frm_br, textvariable=self._var_bitrate,
                                         values=[64, 96, 128, 192, 320],
                                         width=6, state='readonly')
        self._cb_bitrate.pack(side='left')
        ttk.Label(frm_br, text='  кбит/с', foreground=hint_fg).pack(side='left')

        # Непрерывная запись
        ttk.Label(frm, text='Непрерывная запись:').grid(row=8, column=0, **lbl_kw)
        frm_full = ttk.Frame(frm)
        frm_full.grid(row=8, column=1, **wdg_kw)
        self._var_full_record = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_full, variable=self._var_full_record,
                        command=self._on_full_record_toggled).pack(side='left')
        ttk.Label(frm_full,
                  text='  параллельно записывать полный файл full_* без разбивки на части',
                  foreground=hint_fg).pack(side='left')

        # Папка для full_* файлов
        ttk.Label(frm, text='Папка для full_*:').grid(row=9, column=0, **lbl_kw)
        frm_fdir = ttk.Frame(frm)
        frm_fdir.grid(row=9, column=1, **wdg_kw)
        self._var_full_dir = tk.StringVar()
        self._entry_full_dir = ttk.Entry(frm_fdir, textvariable=self._var_full_dir, width=38)
        self._entry_full_dir.pack(side='left')
        self._btn_full_browse = ttk.Button(frm_fdir, text='Обзор',
                                            command=self._browse_full)
        self._btn_full_browse.pack(side='left', padx=(6, 0))
        self._btn_full_open = ttk.Button(frm_fdir, text='📂 Открыть',
                                          command=self._open_full_dir)
        self._btn_full_open.pack(side='left', padx=(4, 0))
        ttk.Label(frm_fdir, text='  (пусто = та же папка что и rec_*)',
                  foreground=hint_fg).pack(side='left')

        # Разделитель
        ttk.Separator(frm, orient='horizontal').grid(
            row=10, column=0, columnspan=2, sticky='ew', pady=(10, 4))

        # Микрофон — выбор устройства
        ttk.Label(frm, text='Микрофон:').grid(row=11, column=0, **lbl_kw)
        frm_mic_dev = ttk.Frame(frm)
        frm_mic_dev.grid(row=11, column=1, **wdg_kw)
        self._var_mic_device = tk.StringVar()
        self._cb_mic = ttk.Combobox(frm_mic_dev, textvariable=self._var_mic_device,
                                     width=56, state='readonly')
        self._cb_mic.pack(side='left')
        ttk.Button(frm_mic_dev, text='↻', width=3,
                   command=self._scan_devices).pack(side='left', padx=(6, 0))

        # Системный звук — выбор устройства
        ttk.Label(frm, text='Системный звук:').grid(row=12, column=0, **lbl_kw)
        frm_sys_dev = ttk.Frame(frm)
        frm_sys_dev.grid(row=12, column=1, **wdg_kw)
        self._var_sys_device = tk.StringVar()
        self._cb_sys = ttk.Combobox(frm_sys_dev, textvariable=self._var_sys_device,
                                     width=56, state='readonly')
        self._cb_sys.pack(side='left')
        ttk.Label(frm_sys_dev,
                  text='  ← для RDP: выбери VB-Cable или нужный loopback',
                  foreground=hint_fg).pack(side='left')

        # ── Controls ──────────────────────────────────────────────────────────
        frm_ctrl = ttk.Frame(self)
        frm_ctrl.grid(row=1, column=0, sticky='ew', pady=(0, 10))

        self._btn = ttk.Button(frm_ctrl, text='  ●  Начать запись  ', command=self._toggle)
        self._btn.pack(side='left', ipadx=12, ipady=5)
        self._var_auto_mic = tk.BooleanVar()
        self._btn_mic_mute = tk.Button(frm_ctrl, text='🎤 Микрофон',
                                        command=self._toggle_mic_mute, state='disabled',
                                        bg='#e0e0e0', activebackground='#d0d0d0',
                                        relief='raised', bd=2, font=('Segoe UI', 9))
        self._btn_mic_mute.pack(side='left', padx=(10, 0), ipadx=8, ipady=5)
        self._chk_auto_mic = tk.Checkbutton(frm_ctrl, text='Авто-вкл. по уровню',
                                             variable=self._var_auto_mic,
                                             state='disabled',
                                             font=('Segoe UI', 9),
                                             fg='#555555', selectcolor='#e8ffe8')
        self._chk_auto_mic.pack(side='left', padx=(8, 0))
        self._lbl_total_time = ttk.Label(frm_ctrl, text='', font=('Consolas', 10),
                                          foreground='#888888')
        self._lbl_total_time.pack(side='right', padx=(0, 12))

        # ── Meters frame ──────────────────────────────────────────────────────
        frm_m = ttk.LabelFrame(self, text='Уровни звука (RMS)', padding=(10, 6))
        frm_m.grid(row=2, column=0, sticky='ew', pady=(0, 8))

        # Микрофон
        ttk.Label(frm_m, text='Микрофон:', width=11, anchor='e').grid(
            row=0, column=0, padx=(0, 6), pady=3)
        self._canvas_mic = tk.Canvas(frm_m, width=METER_W, height=METER_H,
                                     bg='#1e1e1e', highlightthickness=1,
                                     highlightbackground='#555555')
        self._canvas_mic.grid(row=0, column=1, pady=3)
        self._lbl_mic = ttk.Label(frm_m, text='  —  ', width=6,
                                  anchor='e', font=('Consolas', 9))
        self._lbl_mic.grid(row=0, column=2, padx=(6, 0))

        # Динамики
        ttk.Label(frm_m, text='Динамики:', width=11, anchor='e').grid(
            row=1, column=0, padx=(0, 6), pady=3)
        self._canvas_sys = tk.Canvas(frm_m, width=METER_W, height=METER_H,
                                     bg='#1e1e1e', highlightthickness=1,
                                     highlightbackground='#555555')
        self._canvas_sys.grid(row=1, column=1, pady=3)
        self._lbl_sys = ttk.Label(frm_m, text='  —  ', width=6,
                                  anchor='e', font=('Consolas', 9))
        self._lbl_sys.grid(row=1, column=2, padx=(6, 0))

        # Верхний предел
        frm_max = ttk.Frame(frm_m)
        frm_max.grid(row=2, column=0, columnspan=3, sticky='w', pady=(4, 0))
        ttk.Label(frm_max, text='Верхний предел шкалы:').pack(side='left')
        self._var_meter_max = tk.IntVar()
        ttk.Spinbox(frm_max, from_=100, to=32768, increment=100,
                    textvariable=self._var_meter_max, width=7).pack(side='left', padx=(6, 0))
        ttk.Label(frm_max,
                  text='  RMS    │ белая черта = порог тишины │ зелёный < порог, жёлтый ≈ порог, красный > порог',
                  foreground=hint_fg).pack(side='left')

        # ── Status row ────────────────────────────────────────────────────────
        frm_status = ttk.Frame(self)
        frm_status.grid(row=3, column=0, sticky='w', pady=(0, 6))
        self._dot = tk.Label(frm_status, text='●', font=('Segoe UI', 13), fg='#aaaaaa')
        self._dot.pack(side='left')
        self._lbl_status = ttk.Label(frm_status, text='Готов к записи', font=('Segoe UI', 10))
        self._lbl_status.pack(side='left', padx=(6, 0))
        self._lbl_file_time = ttk.Label(frm_status, text='', font=('Consolas', 10),
                                         foreground='#cc0000')
        self._lbl_file_time.pack(side='left', padx=(10, 0))
        self._btn_delete = ttk.Button(frm_status, text='🗑 Удалить',
                                      command=self._delete_recorded, state='disabled')
        self._btn_delete.pack(side='left', padx=(12, 0))
        ttk.Button(frm_status, text='📂 Открыть папку',
                   command=self._open_folder).pack(side='left', padx=(6, 0))

        # ── Saved files list ──────────────────────────────────────────────────
        frm_files = ttk.LabelFrame(self, text='Записанные файлы', padding=6)
        frm_files.grid(row=4, column=0, sticky='nsew', pady=(0, 4))
        self.rowconfigure(4, weight=1)
        self.columnconfigure(0, weight=1)

        self._listbox = tk.Listbox(frm_files, width=72, height=7,
                                   font=('Consolas', 9), activestyle='none')
        sb = ttk.Scrollbar(frm_files, orient='vertical', command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=sb.set)
        self._listbox.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self._listbox.bind('<Double-Button-1>', self._open_file)

        ttk.Label(self, text='Двойной клик по файлу — открыть',
                  foreground='#888888', font=('Segoe UI', 8)
                  ).grid(row=5, column=0, sticky='w')

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
        self._var_full_record.set(s.get('full_record_enabled', False))
        self._var_full_dir.set(s.get('full_output_dir', ''))
        self._on_full_record_toggled()
        # Devices restored in _scan_devices() which is called after _load_into_ui()
        self._var_meter_max.set(s.get('meter_max', 2000))
        self._var_auto_mic.set(s.get('auto_mic_on_level', False))
        self._toggle_min_speech()
        self._on_format_changed()

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
        except Exception as e:
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

    def _on_full_record_toggled(self):
        state = 'normal' if self._var_full_record.get() else 'disabled'
        self._entry_full_dir.config(state=state)
        self._btn_full_browse.config(state=state)

    def _browse_full(self):
        d = filedialog.askdirectory(initialdir=self._var_full_dir.get() or self._var_dir.get())
        if d:
            self._var_full_dir.set(d)

    def _open_full_dir(self):
        d = self._var_full_dir.get().strip() or self._var_dir.get().strip() or str(Path(__file__).parent / 'recordings')
        Path(d).mkdir(parents=True, exist_ok=True)
        os.startfile(d)

    def _on_format_changed(self, *_):
        is_wav = self._var_format.get() == 'wav'
        self._cb_bitrate.config(state='disabled' if is_wav else 'readonly')

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
            'output_dir':           self._var_dir.get().strip(),
            'silence_rms':          int(self._var_rms.get()),
            'silence_duration':     float(self._var_silence_dur.get()),
            'min_speech_enabled':   self._var_min_speech_on.get(),
            'min_speech_duration':  float(self._var_min_speech.get()),
            'min_record_minutes':   float(self._var_min_record.get()),
            'idle_timeout_minutes': float(self._var_idle_timeout.get()),
            'mp3_bitrate':          int(self._var_bitrate.get()),
            'output_format':        self._var_format.get(),
            'full_record_enabled':  self._var_full_record.get(),
            'full_output_dir':      self._var_full_dir.get().strip(),
            'mic_device_index':     self._mic_device_map.get(self._var_mic_device.get(), -1) or -1,
            'sys_device_index':     self._sys_device_map.get(self._var_sys_device.get(), -1) or -1,
            'meter_max':            int(self._var_meter_max.get()),
            'auto_mic_on_level':    self._var_auto_mic.get(),
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

    def _delete_recorded(self):
        wav_files = [f for f in self._saved_files
                     if os.path.isfile(f) and f.lower().endswith('.wav')]
        if not wav_files:
            messagebox.showinfo('Удаление', 'WAV-файлы не найдены.')
            return
        n = len(wav_files)
        if not messagebox.askokcancel('Удалить WAV-файлы',
                                      f'Удалить {n} WAV-файл(ов)?'):
            return
        errors = []
        for f in wav_files:
            try:
                os.remove(f)
            except OSError as e:
                errors.append(f'{os.path.basename(f)}: {e}')
        if errors:
            self._set_status(f'Ошибка удаления: {os.path.basename(errors[0])}', '#cc0000')
        else:
            self._set_status(f'Удалено {n} WAV-файл(ов)', '#555555')
            self._btn_delete.config(state='disabled')

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle(self):
        if not self._recording:
            self._start()
        else:
            self._stop()

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

    def _start(self):
        s = self._collect()
        _save_settings(s)
        self._settings = s
        self._saved_files.clear()
        self._listbox.delete(0, tk.END)
        self._btn_delete.config(state='disabled')
        self._reset_meters()

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
                full_record=s['full_record_enabled'],
                full_output_dir=s['full_output_dir'],
                mic_device=None if s['mic_device_index'] == -1 else s['mic_device_index'],
                sys_device=None if s['sys_device_index'] == -1 else s['sys_device_index'],
                on_status=self._cb_status,
                on_file_saved=self._cb_file,
                on_idle_timeout=self._cb_idle_timeout,
                on_levels=self._cb_levels,
            )
            self._recorder.start()
        except Exception as e:
            messagebox.showerror('Ошибка запуска', str(e))
            return

        self._recording = True
        self._btn.config(text='  ■  Остановить запись  ')
        self._update_mic_btn(False)
        self._btn_mic_mute.config(state='normal')
        self._chk_auto_mic.config(state='normal')
        self._set_status('Ожидание речи...', '#888888')
        self._start_blink('#888888')
        self._start_timer()

    def _stop(self):
        self._recording = False
        self._btn.config(state='disabled', text='  ■  Остановить запись  ')
        self._stop_blink()
        self._set_status('Остановка...', '#e07000')

        def _do():
            files = self._recorder.stop() if self._recorder else []
            self.after(0, self._on_stopped, files)

        threading.Thread(target=_do, daemon=True).start()

    def _on_stopped(self, files: list[str]):
        self._recorder = None
        n = len(files)
        self._btn.config(state='normal', text='  ●  Начать запись  ')
        self._update_mic_btn(False)
        self._btn_mic_mute.config(state='disabled')
        self._chk_auto_mic.config(state='disabled')
        self._dot.config(fg='#aaaaaa')
        self._set_status(f'Готово. Сохранено файлов: {n}',
                         '#005500' if n else '#555555')
        self._btn_delete.config(state='normal' if n else 'disabled')
        self._reset_meters()
        self._stop_timer()

    # ── Callbacks from recorder thread ────────────────────────────────────────

    def _cb_status(self, msg: str):
        def _upd():
            if '[REC]' in msg:
                self._file_start_time = time.monotonic()
                self._set_status('Запись...', '#cc0000')
                self._start_blink('#cc0000')
            elif 'Ожидание' in msg:
                self._file_start_time = None
                self._lbl_file_time.config(text='')
                self._set_status('Ожидание речи...', '#888888')
                self._start_blink('#888888')
            elif 'Saved' in msg:
                self._file_start_time = None
                self._lbl_file_time.config(text='')
                parts = msg.strip().split(': ', 1)
                name = Path(parts[1]).name if len(parts) > 1 else msg.strip()
                self._set_status(f'Сохранено: {name}', '#006600')
        self.after(0, _upd)

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
        self._stop_blink()
        self._btn.config(state='disabled')
        self._set_status('Авто-стоп: долгая тишина...', '#e07000')

        def _do():
            if self._recorder:
                self._recorder.stop()
            self.after(0, self._on_idle_done)

        threading.Thread(target=_do, daemon=True).start()

    def _on_idle_done(self):
        self._recorder = None
        n = len(self._saved_files)
        self._btn.config(state='normal', text='  ●  Начать запись  ')
        self._update_mic_btn(False)
        self._btn_mic_mute.config(state='disabled')
        self._chk_auto_mic.config(state='disabled')
        self._dot.config(fg='#aaaaaa')
        self._set_status(f'Авто-стоп: тишина. Файлов: {n}', '#555555')
        self._reset_meters()
        self._stop_timer()

    # ── Meters ────────────────────────────────────────────────────────────────

    def _draw_meter(self, canvas: tk.Canvas, label: ttk.Label, rms: float, muted: bool = False):
        threshold = float(max(1, self._var_rms.get()))
        max_rms   = float(max(1, self._var_meter_max.get()))

        fill_x = int(METER_W * min(1.0, rms / max_rms))
        thr_x  = int(METER_W * min(1.0, threshold / max_rms))

        if muted:
            color = '#555555'        # серый — микрофон отключён
        elif rms >= threshold:
            color = '#cc3333'        # красный — выше порога
        elif rms >= threshold * 0.65:
            color = '#cc9900'        # жёлтый — приближается к порогу
        else:
            color = '#33aa55'        # зелёный — ниже порога

        canvas.delete('all')
        if fill_x > 0:
            canvas.create_rectangle(0, 0, fill_x, METER_H, fill=color, outline='')
        # Маркер порога тишины
        if 0 < thr_x <= METER_W:
            canvas.create_line(thr_x, 0, thr_x, METER_H, fill='white', width=2)

        label.config(text='ОТКЛ' if muted else f'{int(rms):>5}',
                     foreground='#cc2222' if muted else '')

    def _reset_meters(self):
        for canvas, label in ((self._canvas_mic, self._lbl_mic),
                               (self._canvas_sys, self._lbl_sys)):
            canvas.delete('all')
            label.config(text='  —  ')

    # ── Status / blink ────────────────────────────────────────────────────────

    # ── Timers ────────────────────────────────────────────────────────────────

    def _start_timer(self):
        self._record_start_time = time.monotonic()
        self._file_start_time   = None
        self._tick_timer()

    def _tick_timer(self):
        if not self._recording:
            return
        total = time.monotonic() - self._record_start_time
        tm, ts = divmod(int(total), 60)
        self._lbl_total_time.config(text=f'Общее:  {tm}:{ts:02d}')
        if self._file_start_time is not None:
            ft = time.monotonic() - self._file_start_time
            fm, fs = divmod(int(ft), 60)
            self._lbl_file_time.config(text=f'{fm}:{fs:02d}')
        self._timer_job = self.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_job:
            self.after_cancel(self._timer_job)
            self._timer_job = None
        self._record_start_time = None
        self._file_start_time   = None
        self._lbl_total_time.config(text='')
        self._lbl_file_time.config(text='')

    def _set_status(self, text: str, color: str = ''):
        self._lbl_status.config(text=text, foreground=color)

    def _start_blink(self, color: str):
        self._stop_blink()
        self._blink_color = color
        self._blink_state = True
        self._do_blink()

    def _do_blink(self):
        if not self._recording:
            return
        self._dot.config(fg=self._blink_color if self._blink_state else '#dddddd')
        self._blink_state = not self._blink_state
        self._blink_job = self.after(550, self._do_blink)

    def _stop_blink(self):
        if self._blink_job:
            self.after_cancel(self._blink_job)
            self._blink_job = None

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._recording and self._recorder:
            self._recorder.stop()
        self.destroy()


if __name__ == '__main__':
    app = RecorderApp()
    app.mainloop()
