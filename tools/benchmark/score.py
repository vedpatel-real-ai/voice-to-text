"""Score bench-{small,medium}.json in this directory. Micro-averaged edit rates."""
import json
import re
import statistics
from pathlib import Path
import numpy as np

def norm(s):
    s=s.casefold().replace("’","'")
    replacements={r"\bthree thirty\b|\b3[.:]30\b":"time330",r"\btwenty eighth\b|\b28th\b|\b28\b":"day28",r"\btwelve\b|\b12\b":"qty12",r"\bforty nine dollars and ninety five cents\b|\$49\.95":"usd4995",r"\btwentieth\b|\b20th\b":"ord20",r"\bi'm\b":"i am",r"\bi'll\b":"i will",r"\bem\b":"them",r"\bit's\b":"it is"}
    for p,v in replacements.items():s=re.sub(p,v,s)
    return " ".join(re.findall(r"[a-z0-9]+",s))
def align(a,b):
    d=[[(0,0,0)]*(1+len(b)) for _ in range(len(a)+1)]
    for i in range(len(a)+1):d[i][0]=(0,i,0)
    for j in range(len(b)+1):d[0][j]=(0,0,j)
    for i,x in enumerate(a,1):
        for j,y in enumerate(b,1):
            s,e,h=d[i-1][j-1]; subs=(s+(x!=y),e,h)
            s,e,h=d[i-1][j]; dels=(s,e+1,h)
            s,e,h=d[i][j-1]; ins=(s,e,h+1)
            d[i][j]=min([subs,dels,ins],key=sum)
    return d[-1][-1]
def counts(rows,normalize,chars):
    totals=[0,0,0]; n=0
    for r in rows:
        a,b=r["reference"],r["hypothesis"]
        if normalize:a,b=norm(a),norm(b)
        if not chars:a,b=a.split(),b.split()
        n+=len(a)
        vals=align(a,b);totals=[x+y for x,y in zip(totals,vals)]
    return dict(rate=sum(totals)/n if n else None,ref=n,S=totals[0],D=totals[1],I=totals[2])
def stats(xs):return dict(zip(["min","p50","p90","p95","max"],map(float,np.percentile(xs,[0,50,90,95,100]))),n=len(xs))
def labels(rows,slots):
    hits=total=0
    for r in rows:
        a,b=norm(r["reference"]).split(),norm(r["hypothesis"]).split()
        for word in slots:
            total+=a.count(word);hits+=min(a.count(word),b.count(word))
    return [hits,total]
report={}
for size in ["small","medium"]:
    inp=json.loads(Path("bench-"+size+".json").read_text())
    speech=[r for r in inp["samples"] if r["reference"]]; out={}
    for group in ["all","natural","ksp","awb","libri","tts","perturbed"]:
        rows=[r for r in speech if group=="all" or r["group"]==group or (group=="natural" and r["group"] in ("ksp","awb","libri"))]
        out[group]=dict(n=len(rows),seconds=sum(r["duration_s"] for r in rows),rawWER=counts(rows,False,False),rawCER=counts(rows,False,True),normWER=counts(rows,True,False),normCER=counts(rows,True,True),low=sum(r["outcome"]=="low_confidence" for r in rows),infer=stats([r["inference_s"] for r in rows]),pipeline=stats([r["total_s"] for r in rows]),cpu=stats([r["cpu_s"] for r in rows]),number_slots=labels(rows,["time330","day28","qty12","usd4995","ord20"]),name_slots=labels(rows,["philip","steels","tom","whittemore","phil","maya","jira","github"]),tech_slots=labels(rows,["websocket","api","python","numpy","usb","adapters"]))
    report[size]=out
Path("scores.json").write_text(json.dumps(report,indent=2))
