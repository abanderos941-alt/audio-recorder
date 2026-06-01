"""
Run this first to check available audio devices.
"""
import pyaudiowpatch as pyaudio

p = pyaudio.PyAudio()
print(f"\n{'='*60}")
print("ALL AUDIO DEVICES")
print(f"{'='*60}")
for i in range(p.get_device_count()):
    d = p.get_device_info_by_index(i)
    kind = []
    if d['maxInputChannels'] > 0:
        kind.append(f"IN:{d['maxInputChannels']}ch")
    if d['maxOutputChannels'] > 0:
        kind.append(f"OUT:{d['maxOutputChannels']}ch")
    loopback = " [LOOPBACK]" if d.get('isLoopbackDevice') else ""
    print(f"  [{i:2d}] {d['name']:<45} {', '.join(kind):<15} {int(d['defaultSampleRate'])}Hz{loopback}")

print(f"\n{'='*60}")
print("WASAPI LOOPBACK DEVICES (system audio)")
print(f"{'='*60}")
for lb in p.get_loopback_device_info_generator():
    print(f"  [{lb['index']:2d}] {lb['name']}")

try:
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    mic_idx  = wasapi['defaultInputDevice']
    spk_idx  = wasapi['defaultOutputDevice']
    mic  = p.get_device_info_by_index(mic_idx)
    spk  = p.get_device_info_by_index(spk_idx)
    print(f"\nDefault MIC : [{mic_idx}] {mic['name']}")
    print(f"Default SPK : [{spk_idx}] {spk['name']}")
except Exception as e:
    print(f"\nWASAPI not available: {e}")

p.terminate()
