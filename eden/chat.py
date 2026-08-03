#!/home/haven/vault/repos/eden-os/.venv/bin/python3
"""Eden Chat — Governor + Cortex + 4B. All Eden systems active."""
import json, os, sys, time, uuid, sqlite3, urllib.request
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "/home/haven/vault/repos/eden-os")

from eden.governor.checks import (
    check_sovereignty, check_accords, check_janus, check_boundary,
    check_logging, check_cost, check_self_modify
)
from eden.cortex import classify, get_router

EDEN_4B = "http://localhost:9093/v1"

SYSTEM = """You are Eden — the built-in assistant for the Eden Operating Environment.
A 4B local model. Bootstrap layer. Direct, sharp, capable. Just Eden."""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Eden OE</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a0f;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
header{background:#12121a;padding:10px 20px;border-bottom:1px solid #1e1e2e;display:flex;align-items:center;gap:10px}
header h1{font-size:16px;color:#c4a7e7}header span{font-size:11px;color:#666;margin-left:auto}
.status{display:flex;gap:10px;font-size:10px}
.status .on{color:#4ade80}
main{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:82%;padding:8px 14px;border-radius:12px;line-height:1.5;white-space:pre-wrap;font-size:14px}
.user{align-self:flex-end;background:#2a1f3d;color:#c4a7e7}
.eden{align-self:flex-start;background:#1a1a2e;color:#e0e0e0}
.system{font-size:10px;color:#444;align-self:center;margin:2px 0}
footer{padding:10px 20px;border-top:1px solid #1e1e2e;display:flex;gap:8px}
footer input{flex:1;padding:10px 14px;border:1px solid #2a2a3e;border-radius:8px;background:#12121a;color:#e0e0e0;font-size:14px;outline:none}
footer input:focus{border-color:#7c3aed}
footer button{padding:10px 18px;border:none;border-radius:8px;background:#7c3aed;color:#fff;cursor:pointer;font-size:14px}
footer button:hover{background:#6d28d9}
</style></head>
<body>
<header><h1>Eden OE</h1>
<div class="status"><span class="on">● Gov</span><span class="on">● Cortex</span><span class="on">● 4B</span></div>
<span>v0.18</span></header>
<main id="chat"><div class="system">Eden Operating Environment · Project Glacie LLC</div></main>
<footer>
  <input id="input" placeholder="Talk to Eden..." onkeydown="if(event.key==='Enter')send()" autofocus>
  <button onclick="send()">Send</button>
</footer>
<script>
async function send(){
  const input=document.getElementById('input');
  const msg=input.value.trim();
  if(!msg)return;input.value='';
  addMsg('user',msg);
  const el=addMsg('eden','...');el.style.color='#666';
  try{
    const resp=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    const data=await resp.json();
    el.textContent=data.content||'';el.style.color='#e0e0e0';
    if(data.pipeline){
      const s=document.createElement('div');s.className='system';
      s.textContent='Pipeline: '+data.pipeline.join(' → ');
      document.getElementById('chat').appendChild(s);
    }
  }catch(e){el.textContent=e.message;el.style.color='#ff6b6b';}
}
function addMsg(role,text){const el=document.createElement('div');el.className='msg '+role;el.textContent=text;document.getElementById('chat').appendChild(el);el.scrollIntoView({behavior:'smooth'});return el;}
</script></body></html>"""

app = FastAPI(title="Eden OE")

@app.get("/", response_class=HTMLResponse)
async def index(): return HTML

@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    message = body.get("message", "")
    sid = str(uuid.uuid4())[:12]
    pipeline = []
    
    # 1. Governor checks (direct, no agent object needed)
    checks = [
        check_sovereignty("chat", {"content": message}, "eden"),
        check_accords("chat", None, "eden"),
        check_janus("chat", None),
        check_boundary("chat", "A", "eden"),
        check_logging("chat", None, "eden"),
        check_cost("chat", None),
        check_self_modify("chat", None, "eden"),
    ]
    blocked = [c for c in checks if not c.passed]
    if blocked:
        return {"content": f"Governor: DENIED — {blocked[0].reason}", "pipeline": ["Governor: BLOCKED"]}
    pipeline.append("Governor")
    
    # 2. Cortex
    op = classify(message)
    route = get_router().route(op)
    pipeline.append(f"Cortex:{op.value}")
    
    # 3. 4B response
    t0 = time.time()
    payload = json.dumps({
        "model": "Qwen3.5-4B-Uncensored-Q4_K_M",
        "messages": [
            {"role":"system","content":SYSTEM},
            {"role":"user","content":message}
        ],
        "max_tokens":300,"temperature":0.7,
        "chat_template_kwargs":{"enable_thinking":False}
    }).encode()
    
    req = urllib.request.Request(f"{EDEN_4B}/chat/completions", data=payload, 
                                  headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
        response = data["choices"][0]["message"]["content"]
    pipeline.append(f"4B:{int((time.time()-t0)*1000)}ms")
    
    return {"content": response, "pipeline": pipeline}

if __name__ == "__main__":
    print("Eden OE Chat — Governor + Cortex + 4B")
    uvicorn.run(app, host="127.0.0.1", port=8700, log_level="warning")
