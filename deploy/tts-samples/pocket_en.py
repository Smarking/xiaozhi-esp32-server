import os, time
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["OMP_NUM_THREADS"] = "4"
import torch
torch.set_num_threads(4)
import scipy.io.wavfile
from pocket_tts import TTSModel
from pocket_tts.models.tts_model import get_predefined_voice

t0 = time.time()
model = TTSModel.load_model(language="english")
print(f"[timing] load_model = {time.time()-t0:.2f}s  sr={model.sample_rate}")

t0 = time.time()
voice = model.get_state_for_audio_prompt("alba")  # predefined catalog voice (ungated)
print(f"[timing] get_state (voice) = {time.time()-t0:.2f}s")

sents = [
    "Hello, this is a quick test of Pocket TTS running on a CPU.",
    "The weather today is sunny with a gentle breeze.",
    "I can help you set a reminder, play music, or answer questions.",
    "Thanks for waiting, let's continue our conversation.",
]

# warmup
t0 = time.time()
_ = model.generate_audio(voice, "warm up sentence")
print(f"[timing] warmup = {time.time()-t0:.2f}s")

wavs = []
for i, s in enumerate(sents):
    t0 = time.time()
    audio = model.generate_audio(voice, s)
    dt = time.time() - t0
    dur = audio.shape[-1] / model.sample_rate
    rtf = dt / dur if dur > 0 else float("nan")
    print(f"[sent {i}] gen={dt:.2f}s audio={dur:.2f}s RTF={rtf:.3f}  '{s[:40]}...'")
    wavs.append(audio)

full = torch.cat(wavs, dim=-1)
scipy.io.wavfile.write("/work/pocket_en.wav", model.sample_rate, full.numpy())
print("[done] wrote /work/pocket_en.wav  total_dur=%.2fs" % (full.shape[-1]/model.sample_rate))
