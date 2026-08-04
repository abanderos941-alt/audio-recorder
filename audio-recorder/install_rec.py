"""
Установщик зависимостей для аудио-рекордера.

Проверяет требования системы (Windows, версия Python, tkinter, pip),
устанавливает недостающие пакеты из requirements.txt и проверяет, что
после установки всё импортируется и WASAPI видит хотя бы одно устройство.
Ничего не удаляет, не трогает recorder_settings.json и существующие записи.

Запуск:
    python install_rec.py
"""
from __future__ import annotations

import importlib
import platform
import subprocess
import sys
from pathlib import Path

REQUIRED_PACKAGES = {
    'pyaudiowpatch': 'pyaudiowpatch>=0.2.12',
    'lameenc':       'lameenc>=1.7',
    'numpy':         'numpy>=1.24',
}
MIN_PYTHON = (3, 10)
HERE = Path(__file__).parent


def _ok(msg: str) -> None:
    print(f'  [OK]   {msg}')


def _warn(msg: str) -> None:
    print(f'  [!]    {msg}')


def _fail(msg: str) -> None:
    print(f'  [FAIL] {msg}')


def _step(msg: str) -> None:
    print(f'\n{msg}')


def check_platform() -> bool:
    _step('Проверка ОС...')
    if platform.system() != 'Windows':
        _fail(f'Обнаружена {platform.system()}, а не Windows.')
        print('  pyaudiowpatch использует WASAPI и работает только на Windows.')
        return False
    _ok(f'Windows {platform.release()} ({platform.machine()})')
    return True


def check_python_version() -> bool:
    _step('Проверка версии Python...')
    cur = sys.version_info[:2]
    if cur < MIN_PYTHON:
        _fail(f'Python {cur[0]}.{cur[1]} слишком старый, нужен {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+.')
        print('  Скачать: https://www.python.org/downloads/')
        return False
    _ok(f'Python {sys.version.split()[0]}')
    return True


def check_tkinter() -> bool:
    _step('Проверка tkinter (GUI)...')
    try:
        import tkinter  # noqa: F401
        _ok('tkinter доступен')
        return True
    except ImportError:
        _fail('tkinter не найден.')
        print('  tkinter не ставится через pip — это часть самого установщика Python.')
        print('  Переустанови Python с python.org, оставив галку')
        print('  "tcl/tk and IDLE" включённой при установке.')
        return False


def check_pip() -> bool:
    _step('Проверка pip...')
    try:
        subprocess.run([sys.executable, '-m', 'pip', '--version'],
                        check=True, capture_output=True, text=True)
        _ok('pip доступен')
        return True
    except Exception as e:
        _fail(f'pip недоступен: {e}')
        print('  Попробуй: python -m ensurepip --upgrade')
        return False


def installed_packages() -> dict[str, bool]:
    result = {}
    for mod_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod_name)
            result[mod_name] = True
        except ImportError:
            result[mod_name] = False
    return result


def install_missing(missing: list[str]) -> bool:
    _step('Установка недостающих пакетов: ' + ', '.join(missing) + '...')
    specs = [REQUIRED_PACKAGES[name] for name in missing]
    proc = subprocess.run([sys.executable, '-m', 'pip', 'install', *specs])
    if proc.returncode != 0:
        _fail('pip install завершился с ошибкой (см. вывод выше).')
        return False
    _ok('Установка завершена')
    return True


def verify_imports() -> bool:
    _step('Проверка импорта после установки...')
    importlib.invalidate_caches()  # иначе только что поставленный пакет может не найтись в этом же процессе
    all_ok = True
    for mod_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod_name)
            _ok(f'{mod_name} импортируется')
        except ImportError as e:
            _fail(f'{mod_name} не импортируется: {e}')
            all_ok = False
    return all_ok


def check_audio_devices() -> None:
    _step('Проверка аудио-устройств (WASAPI)...')
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        _warn('Пропущено — pyaudiowpatch не установлен.')
        return
    try:
        pa = pyaudio.PyAudio()
        mic_count = sum(1 for i in range(pa.get_device_count())
                         if pa.get_device_info_by_index(i)['maxInputChannels'] > 0)
        loopback_count = sum(1 for _ in pa.get_loopback_device_info_generator())
        pa.terminate()
        if mic_count == 0:
            _warn('Не найдено ни одного устройства ввода (микрофона).')
        else:
            _ok(f'Найдено устройств ввода: {mic_count}')
        if loopback_count == 0:
            _warn('Не найдено WASAPI loopback-устройств — запись системного '
                  'звука работать не будет (только микрофон).')
        else:
            _ok(f'Найдено loopback-устройств (системный звук): {loopback_count}')
    except Exception as e:
        _warn(f'Не удалось перечислить устройства: {e}')


def confirm(prompt: str) -> bool:
    try:
        answer = input(f'{prompt} [y/N]: ').strip().lower()
    except EOFError:
        return False
    return answer in ('y', 'yes', 'д', 'да')


def main() -> int:
    print('=' * 60)
    print('Установка аудио-рекордера')
    print('=' * 60)

    if not check_platform() or not check_python_version():
        _step('Критические требования не выполнены — установка прервана.')
        return 1

    tkinter_ok = check_tkinter()

    if not check_pip():
        _step('Без pip установить зависимости не получится — установка прервана.')
        return 1

    missing = [name for name, ok in installed_packages().items() if not ok]
    if not missing:
        _step('Все Python-пакеты уже установлены.')
    else:
        _step('Не хватает следующих пакетов:')
        for name in missing:
            print(f'  - {REQUIRED_PACKAGES[name]}')
        if confirm('\nСкачать и установить их сейчас?'):
            if not install_missing(missing):
                return 1
        else:
            _step('Установка пропущена — пакеты не скачаны и не установлены.')

    imports_ok = verify_imports()
    check_audio_devices()

    print('\n' + '=' * 60)
    if imports_ok and tkinter_ok:
        print('Готово. Можно запускать: python recorder_ui.py')
        print('=' * 60)
        return 0
    print('Установка завершена с предупреждениями — см. выше.')
    print('=' * 60)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
