"""Deterministic per-call fixtures for the newly verified readers."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = int(datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)


def codex_call(i):
    ts = BASE + (i % 28) * 86400000 + (i // 28) * 20000
    stamp = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
    usage = dict(input_tokens=100, cached_input_tokens=20, output_tokens=60,
                 reasoning_output_tokens=20, total_tokens=160)
    total = {k: v * (i + 1) for k, v in usage.items()}
    def event(kind, **p):
        return dict(type='event_msg', timestamp=stamp, payload=dict(type=kind, **p))
    records = [event('task_started', turn_id=f't{i}'),
               dict(type='turn_context',timestamp=stamp,payload=dict(turn_id=f't{i}',model='gpt-6'))]
    for kind, lo, hi, identity in [('Reasoning',0,500,'r'),('AgentMessage',500,2500,'a'),
                                  ('CommandExecution',800,1200,'tool'),('McpToolCall',1000,1500,'tool2')]:
        records.append(event('item_completed',turn_id=f't{i}',
            item=dict(type=kind,id=f'{identity}{i}'),started_at_ms=ts+lo,completed_at_ms=ts+hi))
    records.extend([dict(type='token_usage_record',timestamp=stamp,
                        payload=dict(turn_id=f't{i}',response_id=f'resp{i}',usage=usage,thread_token_usage=total)),
                    event('token_count',info=dict(last_token_usage=usage,total_token_usage=total))])
    return records


def build(root, calls=10000):
    root = Path(root)
    codex = root/'codex/sessions/2026/09/01/rollout-bench.jsonl'
    codex.parent.mkdir(parents=True,exist_ok=True)
    with codex.open('w') as f:
        f.write(json.dumps(dict(type='session_meta',payload=dict(id='bench-codex',cwd='/fixture')))+'\n')
        for i in range(calls):
            f.writelines(json.dumps(r)+'\n' for r in codex_call(i))
    dsh=root/'dsh/sessions/project/session-bench.jsonl';dsh.parent.mkdir(parents=True,exist_ok=True)
    with dsh.open('w') as f:
        f.write(json.dumps(dict(type='session',version=4,id='bench-dsh',createdAt=BASE,cwd='/fixture'))+'\n')
        f.write(json.dumps(dict(type='request/context',seq=0,time=BASE,data=dict(model='deepseek-v4-flash',provider='deepseek')))+'\n')
        for i in range(calls//10):
            t=BASE+(i%28)*86400000+i*10000
            usage=dict(inputTokens=100,outputTokens=60,reasoningTokens=20)
            stream=[dict(type='chunk',time=t,chunk=dict(type='reasoning',text='fixture')),
                    dict(type='chunk',time=t+1500,chunk=dict(type='usage',usage=usage)),
                    dict(type='chunk',time=t+1501,chunk=dict(type='finish',reason=dict(kind='stop')))]
            f.write(json.dumps(dict(type='assistant/message',seq=i+1,time=t+1501,
                data=dict(turn=i,step=0,usage=usage,stream=stream,message=dict(role='assistant',source=dict(kind='model',model='deepseek-v4-flash',provider='deepseek')))))+'\n')
    qwen=root/'qwen/projects/project/chats/bench.jsonl';qwen.parent.mkdir(parents=True,exist_ok=True)
    with qwen.open('w') as f:
        for i in range(calls//10):
            t=BASE+(i%28)*86400000+i*10000
            scope=dict(sessionId='bench-qwen',timestamp=datetime.fromtimestamp(t/1000,timezone.utc).isoformat())
            telemetry=dict(type='system',uuid=f'clock{i}',**scope,systemPayload=dict(uiEvent={
                'event.name':'qwen-code.api_response','response_id':f'resp{i}','model':'qwen3-max',
                'duration_ms':2000,'ttft_ms':500,'input_token_count':100,'output_token_count':40,'thoughts_token_count':20,'total_token_count':160}))
            response=dict(type='assistant',uuid=f'answer{i}',parentUuid=f'clock{i}',model='qwen3-max',**scope,
                usageMetadata=dict(promptTokenCount=100,candidatesTokenCount=40,thoughtsTokenCount=20,totalTokenCount=160))
            f.write(json.dumps(telemetry)+'\n'+json.dumps(response)+'\n')
    for source in ('opencode','kilocode','mimo'):
        db=root/f'{source}.db'
        conn=sqlite3.connect(db)
        conn.executescript('''CREATE TABLE project(id TEXT PRIMARY KEY,worktree TEXT);
            CREATE TABLE session(id TEXT PRIMARY KEY,project_id TEXT,title TEXT,directory TEXT,time_created INTEGER,time_updated INTEGER);
            CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);
            CREATE INDEX message_time_created_idx ON message(time_created);
            INSERT INTO project VALUES('p','/fixture');
            INSERT INTO session VALUES('s','p','fixture','/fixture',0,0);''')
        for i in range(calls):
            t=BASE+(i%28)*86400000+(i//28)*10000
            data=dict(role='assistant',modelID='qwen3-max',tokens=dict(input=100,output=40,reasoning=20,cache=dict(read=0,write=0)),
                      time=dict(created=t,completed=t+2000),finish='stop')
            if i%50==0:data['error']=dict(name='AbortError')
            conn.execute('INSERT INTO message VALUES(?,?,?,?,?)',(f'm{i}','s',t,t+2000,json.dumps(data)))
        conn.commit();conn.close()
    return dict(codex_calls=calls,dsh_calls=calls//10,qwen_calls=calls//10,native_calls_per_source=calls,
                codex_file=str(codex),dsh_file=str(dsh),qwen_file=str(qwen))
