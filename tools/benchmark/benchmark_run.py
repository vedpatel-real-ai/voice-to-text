"""Run from a temporary fixture directory, with the reviewed repo on PYTHONPATH.
Arguments: cached model snapshot directory and output JSON file.
The original media and signed download URLs are deliberately excluded from evidence.
"""
import hashlib
import json
import resource
import subprocess
import sys
import time
from pathlib import Path
import numpy as np
import mlx.core as mx
from audio import Capture
from config import DEFAULTS
from transcription import Transcriber

model_path, output = sys.argv[1:]
settings = {**DEFAULTS, "model": model_path}
engine = Transcriber(settings, "GitHub, WebSocket, Python, NumPy, MLX, Jira")
rng = np.random.default_rng(20261126)
cases = []
def decode(path):
    pcm = subprocess.check_output(["ffmpeg","-v","error","-i",str(path),"-ac","1","-ar","16000","-f","f32le","-"])
    return np.frombuffer(pcm,dtype="<f4").copy()
for split in ("ksp","awb"):
    for i,r in enumerate(json.loads(Path(split+".json").read_text())):
        cases.append((r["file"],split,r["text"],decode(split+"-"+str(i)+".media")))
for e in json.loads(Path("libri.json").read_text())["rows"]:
    r=e["row"]
    cases.append((r["id"],"libri",r["text"],decode(r["id"]+".media")))
prompts = [
    ("time","Schedule a meeting at three thirty.",185),
    ("tech","Create a pull request for the WebSocket API on GitHub.",220),
    ("numbers","On September twenty eighth, order twelve USB C adapters for forty nine dollars and ninety five cents.",155),
    ("names","Open Jira and ask Maya to review the Python and NumPy changes.",185),
    ("long","First review the pull request. Then check the numbers carefully. After the meeting, send the revised estimate to the team and schedule another review for Monday.",140),
]
for name,ref,rate in prompts:
    path=Path(name+".aiff")
    if not path.exists():
        subprocess.run(["/usr/bin/say","-v","Samantha","-r",str(rate),"-o",str(path),ref],check=True)
    data=decode(path)
    if len(data)<1600: raise ValueError("empty TTS fixture "+name)
    cases.append((name,"tts",ref,data))
base=cases[17][3]
for name,y in [("quiet",0.025*base),("loud",np.clip(3*base,-1,1)),("noise",base+rng.normal(0,.012,len(base))),("pause",np.concatenate((base,np.zeros(12000),base)))]:
    cases.append((name,"perturbed",prompts[0][1]*(1) if name!="pause" else " ".join([prompts[0][1]]*2),np.array(y,np.float32)))
for name,data in [("silence",np.zeros(32000)),("room",rng.normal(0,.0002,32000)),("click",np.r_[np.zeros(15000),np.ones(20),np.zeros(17000)])]:
    cases.append((name,"control","",np.array(data,np.float32)))
def mem():
    return dict(maxrss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,active_mlx_bytes=mx.get_active_memory(),peak_mlx_bytes=mx.get_peak_memory(),cache_mlx_bytes=mx.get_cache_memory())
before=mem()
start=time.perf_counter()
if not engine.warm_up(): raise RuntimeError(engine.snapshot().error)
results=dict(model=Path(model_path).parents[1].name,revision=Path(model_path).name,warmup_s=time.perf_counter()-start,before=before,after_warmup=mem(),device=str(mx.default_device()),samples=[])
for name,group,ref,data in cases:
    cap=Capture(data,len(data)/16000,float(np.max(np.abs(data))),float(np.sqrt(np.mean(data**2))))
    t=time.perf_counter()
    c=time.process_time()
    r=engine.transcribe(cap)
    results["samples"].append(dict(id=name,group=group,reference=ref,hypothesis=r.text,outcome=r.outcome.value,detail=r.detail,duration_s=cap.duration,pcm_sha256=hashlib.sha256(data.tobytes()).hexdigest(),total_s=time.perf_counter()-t,cpu_s=time.process_time()-c,inference_s=r.seconds,**mem()))
Path(output).write_text(json.dumps(results,indent=2))
print(results["model"],len(cases),results["warmup_s"],mem())
