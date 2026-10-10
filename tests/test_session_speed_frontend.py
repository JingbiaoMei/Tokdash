"""Session speed UI lifecycle, measured populations and response chart semantics."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import tokdash

SRC = (Path(tokdash.__file__).parent / "static" / "index.html").read_text()


def function(name: str) -> str:
    start = SRC.index(name)
    opening = SRC.index("{", SRC.index(")", start))
    depth = 0
    for index in range(opening, len(SRC)):
        depth += (SRC[index] == "{") - (SRC[index] == "}")
        if depth == 0:
            return SRC[start:index + 1]
    raise AssertionError(name)


def run_js(tmp_path, program):
    if not shutil.which("node"):
        pytest.skip("Node unavailable")
    path = tmp_path / "session_speed.js"
    path.write_text(program)
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


STUBS = """
function t(key) { return key; }
function formatNumber(value) { return String(value); }
function formatPercent(value) { return `${Math.round(value*100)}%`; }
function usageReportSpeedFormatRate(value) { return value === null ? '—' : String(value); }
function usageReportSpeedKindLabel(value) { return value || '—'; }
function usageReportSpeedBasisLabel(value) { return value || '—'; }
"""


def test_all_explorer_columns_align_and_do_not_offer_incomparable_ranking():
    section = SRC[SRC.index("<!-- Sessions Tab -->"):SRC.index("<!-- Stats Tab -->")]
    tables = re.findall(r'<table[^>]*data-panel="[^"]+">(.*?)</table>', section, re.S)
    assert len(tables) == 21
    for table in tables:
        header = table[table.index("<thead>"):table.index("</thead>")]
        assert header.count('data-i18n="sessionSpeedColRate"') == 1
        assert header.index('data-i18n="output"') < header.index('data-i18n="sessionSpeedColRate"') < header.index('data-i18n="total"')
        assert "data-sortable" not in next(line for line in header.splitlines() if 'data-i18n="sessionSpeedColRate"' in line)
        count = len(re.findall(r"<th\b", header))
        assert len(re.findall(r"<col\b", table[:table.index("</colgroup>")])) == count
        assert f'colspan="{count}"' in table
    renderer = function("function updateSessionPanel(")
    assert "showTool ? 12 : 11" in renderer
    assert 'createSessionSpeedCell(session, prefix)' in renderer
    assert 'Project speeds are not additive. Leave the range speed cell blank.' in renderer


def test_chart_preserves_equal_clock_responses_gaps_methods_and_streams(tmp_path):
    rows = [
        {"sequence": 1, "stream_id": "main", "recorded_at_ms": 10000, "model": "a", "measurement_kind": "response_window", "token_basis": "output_including_reasoning", "measurement_status": "measured", "output_tok_per_s": 10},
        {"sequence": 2, "stream_id": "main", "recorded_at_ms": 10000, "model": "a", "measurement_kind": "response_window", "token_basis": "output_including_reasoning", "measurement_status": "measured", "output_tok_per_s": 20},
        {"sequence": 3, "stream_id": "main", "recorded_at_ms": 20000, "model": "a", "measurement_status": "missing_timing", "output_tok_per_s": None},
        {"sequence": 4, "stream_id": "main", "recorded_at_ms": 30000, "model": "b", "measurement_kind": "response_window", "token_basis": "output_including_reasoning", "measurement_status": "measured", "output_tok_per_s": 30},
        {"sequence": 5, "stream_id": "main", "recorded_at_ms": 100000, "model": "a", "measurement_kind": "response_window", "token_basis": "output_including_reasoning", "measurement_status": "measured", "output_tok_per_s": 40},
        {"sequence": 1, "stream_id": "agent", "recorded_at_ms": 11000, "model": "a", "measurement_kind": "response_window", "token_basis": "output_including_reasoning", "measurement_status": "measured", "output_tok_per_s": 50},
    ]
    program = "\n".join([function("function sessionSpeedRate("), function("function sessionSpeedGroupKey("), function("function buildSessionSpeedPlot(")])
    result = run_js(tmp_path, program + f"\nconst rows={json.dumps(rows)};console.log(JSON.stringify(buildSessionSpeedPlot(rows,sessionSpeedGroupKey(rows[0]))));")
    assert result["useTime"] is True
    assert result["origin"] == 10000
    assert [point["y"] for point in result["streams"][0]["points"]] == [10, 20, None, None, 40]
    assert [point["x"] for point in result["streams"][0]["points"]] == [0, 0, 10, 20, 90]
    assert [point["y"] for point in result["streams"][1]["points"]] == [50]
    renderer = function("function renderSessionSpeedTimeline(")
    assert "spanGaps: false" in renderer
    assert "tension: 0" in renderer
    assert "buildTimeCumulativeSeries" not in renderer
    assert "selected.output_tok_per_s" in renderer
    assert "createChart" in renderer


def test_missing_timestamp_uses_labelled_response_order_not_an_invented_clock(tmp_path):
    program = "\n".join([function("function sessionSpeedRate("), function("function sessionSpeedGroupKey("), function("function buildSessionSpeedPlot(")])
    program += """
const rows=[{sequence:7,model:'a',stream_id:'main',recorded_at_ms:null,measurement_status:'measured',output_tok_per_s:12}];
console.log(JSON.stringify(buildSessionSpeedPlot(rows,sessionSpeedGroupKey(rows[0]))));
"""
    result = run_js(tmp_path, program)
    assert result["useTime"] is False
    assert result["streams"][0]["points"][0]["x"] == 7
    assert result["streams"][0]["points"][0]["y"] == 12
    assert "sessionSpeedResponseOrder" in function("function renderSessionSpeedTimeline(")


def test_column_distinguishes_pending_absent_error_mixed_partial_and_stale(tmp_path):
    cases = [None, {"cache_state": "ready", "measurement_status": "timing_unavailable"},
             {"cache_state": "error"}, {"cache_state": "ready", "measurement_status": "unsupported_reader"},
             {"cache_state": "ready", "measurement_status": "mixed", "output_tok_per_s": 99},
             {"cache_state": "stale", "output_tok_per_s": 0, "speed_calls": 1, "eligible_calls": 2, "coverage": .5, "partial_model_scope": True}]
    program = STUBS + function("function sessionSpeedRate(") + function("function sessionSpeedSummaryState(")
    result = run_js(tmp_path, program + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(sessionSpeedSummaryState)));")
    assert [r["label"] for r in result] == ["sessionSpeedPending", "—", "sessionSpeedError", "—", "sessionSpeedMixed", "0"]
    assert "sessionSpeedUnsupported" in result[3]["details"]
    assert result[4]["rate"] is None
    assert result[5]["stale"] is True
    assert "sessionSpeedPartialModels" in result[5]["details"]
    assert "sessionSpeedSelectedRange" in result[5]["details"]


def test_hidden_collapsed_and_offscreen_rows_are_not_visible(tmp_path):
    program = function("function sessionSpeedRowVisible(") + """
global.document={hidden:false};global.window={innerHeight:500};const sessionSpeedListState={observer:null};function isSessionsActive(){return true;}
const row={isConnected:true,parentElement:{tagName:'DETAILS',open:true,style:{}},getBoundingClientRect:()=>({height:20,top:100,bottom:120})};
const result=[sessionSpeedRowVisible(row)];row.parentElement.open=false;result.push(sessionSpeedRowVisible(row));
row.parentElement.open=true;row.getBoundingClientRect=()=>({height:20,top:600,bottom:620});result.push(sessionSpeedRowVisible(row));
row.getBoundingClientRect=()=>({height:20,top:100,bottom:120});document.hidden=true;result.push(sessionSpeedRowVisible(row));
console.log(JSON.stringify(result));
"""
    assert run_js(tmp_path, program) == [True, False, False, False]


def batch_program():
    return "\n".join([function("function sessionSpeedListKey("), function("async function loadVisibleSessionSpeeds(")]) + """
const LOCAL_HOST_ID='local'; const currentStartDate='2026-10-01',currentEndDate='2026-10-09';
const sessionSpeedListState={epoch:1,read:new Set(),cache:new Map(),controllers:new Set(),jobs:new Map()};
function sessionSpeedListContext(){return 'range';}function isSessionsActive(){return true;}
function usageReportSpeedWindow(){return {date_from:currentStartDate,date_to:currentEndDate};}
function renderSessionSpeedCell(){} function sessionSpeedRowVisible(row){return row.visible;}
const cells=[];const requests=[];
global.document={hidden:false,querySelectorAll:()=>cells};
function cell(server,tool,id,visible=true){return {_speedServer:{id:server},_speedSession:{tool,session_id:id},closest:()=>({visible})};}
"""


def test_visible_batch_deduplicates_rows_and_keeps_same_ids_on_servers_separate(tmp_path):
    program = batch_program() + """
cells.push(cell('s1','codex','same'),cell('s1','codex','same'),cell('s2','codex','same'),cell('s1','kimi','hidden',false));
async function fetchFromHost(server,path,options){const body=JSON.parse(options.body);requests.push({server:server.id,path,body});return {measurement_generation:2,sessions:body.sessions.map(s=>({...s,cache_state:'ready',output_tok_per_s:12}))};}
(async()=>{await loadVisibleSessionSpeeds();console.log(JSON.stringify({requests,keys:[...sessionSpeedListState.cache.keys()]}));})();
"""
    result = run_js(tmp_path, program)
    assert len(result["requests"]) == 2
    assert all(row["path"] == "/api/session-speeds" for row in result["requests"])
    assert all(row["body"]["sessions"] == [{"tool": "codex", "session_id": "same"}] for row in result["requests"])
    assert len(result["keys"]) == 2


def test_collapsed_list_and_late_range_response_do_not_read_build_or_paint(tmp_path):
    program = batch_program() + """
cells.push(cell('s1','codex','hidden',false));
let resolve;
async function fetchFromHost(server,path,options){requests.push(path);return await new Promise(done=>resolve=done);}
(async()=>{await loadVisibleSessionSpeeds();const hiddenCount=requests.length;
 cells.length=0;cells.push(cell('s1','codex','visible'));const pending=loadVisibleSessionSpeeds();
 sessionSpeedListState.epoch+=1;resolve({sessions:[{tool:'codex',session_id:'visible',cache_state:'not-indexed'}]});await pending;
 console.log(JSON.stringify({hiddenCount,requests,cached:sessionSpeedListState.cache.size}));})();
"""
    result = run_js(tmp_path, program)
    assert result == {"hiddenCount": 0, "requests": ["/api/session-speeds"], "cached": 0}


def test_collapsing_before_cached_read_returns_prevents_build_dispatch(tmp_path):
    program = batch_program() + """
let visible=true;cells.push({_speedServer:{id:'s1'},_speedSession:{tool:'codex',session_id:'a'},closest:()=>({visible})});
async function fetchFromHost(server,path,options){requests.push(path);visible=false;return {sessions:[{tool:'codex',session_id:'a',cache_state:'not-indexed'}]};}
(async()=>{await loadVisibleSessionSpeeds();console.log(JSON.stringify(requests));})();
"""
    assert run_js(tmp_path, program) == ["/api/session-speeds"]


@pytest.mark.parametrize('terminal_state', ['ready', 'error'])
def test_shared_list_job_rereads_visible_snapshot_without_reensuring_stale_rows(tmp_path, terminal_state):
    program = batch_program() + """
for(let i=0;i<52;i++)cells.push(cell('s1','codex',String(i)));
let published=false;const callbacks=[];
function subscribeSessionSpeedJob(server,job,current,complete,failure){callbacks.push({current,complete,failure});return ()=>{};}
async function fetchFromHost(server,path,options){
 const body=JSON.parse(options.body);requests.push({path,body});
 if(path==='/api/output-speed/ensure')return {cache:{state:'building',job_id:'shared'}};
 return {measurement_generation:published?2:1,sessions:body.sessions.map(s=>({...s,cache_state:published?'stale':'not-indexed',speed_calls:published?1:0}))};
}
(async()=>{
 await loadVisibleSessionSpeeds();const before=requests.length;
 const joinedKeys=sessionSpeedListState.jobs.get('s1:shared').readKeys.length;
 cells.push(cell('s1','kimi','unrelated'));published=true;
 if(TERMINAL==='error')callbacks[0].failure({state:'error'});else callbacks[0].complete({state:'ready'});
 await new Promise(done=>setImmediate(done));
 const terminal=requests.slice(before),cached=sessionSpeedListState.cache.size,retired=sessionSpeedListState.jobs.size;
 await loadVisibleSessionSpeeds();
 console.log(JSON.stringify({before,joinedKeys,subscriptions:callbacks.length,terminal,cached,retired,explicit:requests.slice(before+terminal.length)}));
})();
""".replace('TERMINAL', json.dumps(terminal_state))
    out = run_js(tmp_path, program)
    assert out['before'] == 4 and out['joinedKeys'] == 52
    assert len(out['terminal']) == 2 and all(r['path'] == '/api/session-speeds' for r in out['terminal'])
    assert [len(r['body']['sessions']) for r in out['terminal']] == [50, 2]
    assert out['cached'] == 52 and out['retired'] == 0
    assert [r['path'] for r in out['explicit']] == ['/api/session-speeds', '/api/output-speed/ensure']
    assert all(r['body']['sessions'] == [{'tool': 'kimi', 'session_id': 'unrelated'}] for r in out['explicit'])


def test_terminal_list_snapshot_does_not_read_rows_after_collapse(tmp_path):
    program = batch_program() + """
let visible=true;cells.push({_speedServer:{id:'s1'},_speedSession:{tool:'codex',session_id:'a'},closest:()=>({visible})});
let complete;
function subscribeSessionSpeedJob(server,job,current,onComplete){complete=onComplete;return ()=>{};}
async function fetchFromHost(server,path,options){requests.push(path);return path==='/api/output-speed/ensure'
 ?{cache:{state:'building',job_id:'shared'}}:{sessions:[{tool:'codex',session_id:'a',cache_state:'not-indexed'}]};}
(async()=>{await loadVisibleSessionSpeeds();visible=false;complete({state:'ready'});await new Promise(done=>setImmediate(done));console.log(JSON.stringify(requests));})();
"""
    assert run_js(tmp_path, program) == ['/api/session-speeds', '/api/output-speed/ensure']


@pytest.mark.parametrize('terminal_state', ['ready', 'error'])
def test_terminal_modal_job_and_selected_group_read_snapshot_only(tmp_path, terminal_state):
    program = function('async function loadSessionSpeedTimeline(') + """
const sessionSpeedModalState={token:1,tool:'codex',sessionId:'session',server:{id:'one'},loading:false,group:JSON.stringify(['m','response_window','output_including_reasoning'])};
const requests=[],callbacks=[],painted=[];
function sessionSpeedModalCurrent(token){return token===sessionSpeedModalState.token;}
function renderSessionSpeedTimeline(payload){painted.push(payload.cache.state);}
function subscribeSessionSpeedJob(server,job,current,complete,failure){callbacks.push({current,complete,failure});return ()=>{};}
async function fetchFromHost(server,path){requests.push(path);return requests.length===1
 ?{cache:{state:'building',job_id:'one'},total_responses:10}
 :{cache:{state:TERMINAL==='error'?'error':'stale',job_id:'unexpected'},total_responses:2000,truncated:true};}
(async()=>{
 await loadSessionSpeedTimeline(1);
 if(TERMINAL==='error')callbacks[0].failure({state:'error'});else callbacks[0].complete({state:'ready'});
 await new Promise(done=>setImmediate(done));
 const terminal={requests:[...requests],subscriptions:callbacks.length,painted:[...painted]};
 sessionSpeedModalState.token=2;callbacks[0].complete({state:'ready'});await new Promise(done=>setImmediate(done));
 const afterNavigation=requests.length;
 await loadSessionSpeedTimeline(2,true,sessionSpeedModalState.requestedGroup);
 console.log(JSON.stringify({terminal,afterNavigation,manual:requests.at(-1)}));
})();
""".replace('TERMINAL', json.dumps(terminal_state))
    out = run_js(tmp_path, program)
    paths = out['terminal']['requests']
    assert len(paths) == 3 and 'cache_only=1' not in paths[0]
    assert all('cache_only=1' in path and 'refresh=1' not in path for path in paths[1:])
    assert '&model=m' not in paths[1] and '&model=m' in paths[2]
    assert out['terminal']['subscriptions'] == 1 and out['afterNavigation'] == 3
    assert out['terminal']['painted'] == ['building', 'error' if terminal_state == 'error' else 'stale', 'error' if terminal_state == 'error' else 'stale']
    assert 'refresh=1' in out['manual'] and 'cache_only=1' not in out['manual']


def test_modal_speed_response_cannot_repaint_new_session_or_closed_modal(tmp_path):
    program = function("async function loadSessionSpeedTimeline(") + """
const sessionSpeedModalState={token:1,tool:'codex',sessionId:'a',server:{id:'one'},loading:false};
let visible=true;const pending=[];const painted=[];
function sessionSpeedModalCurrent(token){return token===sessionSpeedModalState.token && visible;}
function renderSessionSpeedTimeline(payload){painted.push(payload.tool);}
async function fetchFromHost(){return new Promise(resolve=>pending.push(resolve));}
(async()=>{const a=loadSessionSpeedTimeline(1);Object.assign(sessionSpeedModalState,{token:2,tool:'kimi',sessionId:'b',loading:false});
 const b=loadSessionSpeedTimeline(2);pending[0]({tool:'codex',cache:{state:'ready'}});await a;
 pending[1]({tool:'kimi',cache:{state:'ready'}});await b;
 Object.assign(sessionSpeedModalState,{token:3,loading:false});const closed=loadSessionSpeedTimeline(3);visible=false;pending[2]({tool:'closed',cache:{state:'ready'}});await closed;
 console.log(JSON.stringify(painted));})();
"""
    assert run_js(tmp_path, program) == ["kimi"]
    opener = function("async function openSessionModal(")
    assert opener.index("loadSessionSpeedTimeline(token)") < opener.index("await fetchFromHost")
    assert opener.index("if (!sessionModalCurrent(token)) return;") < opener.index("activeSessionDetail = { tool, data")
    assert "retireSessionSpeedModal()" in function("function closeSessionModal(")


def test_subscriptions_are_retired_on_visibility_close_and_collapse():
    assert "sessionSpeedModalState.controller?.abort()" in function("function retireSessionSpeedModal(")
    assert "sessionSpeedModalState.stopJob?.()" in function("function retireSessionSpeedModal(")
    explorer = function("function initSessionSpeedExplorer(")
    assert "visibilitychange" in explorer
    assert "retireSessionSpeedList()" in explorer
    assert "sessionSpeedModalState.stopJob?.()" in explorer
    assert "stop.isCurrent" in function("function scheduleSessionSpeedList(")
    assert "setInterval" not in explorer


def test_every_new_session_speed_string_has_all_six_translations():
    keys = set(re.findall(r"t\('(?P<key>sessionSpeed\w+)'\)", SRC))
    keys |= set(re.findall(r'data-i18n(?:-aria|-title)?="(sessionSpeed\w+)"', SRC))
    assert len(keys) >= 25
    for key in keys:
        assert len(re.findall(rf"^        {key}: ", SRC, re.M)) == 6, key


def test_keyboard_chart_points_expose_values_skip_gaps_and_navigate_responses(tmp_path):
    program = function("function wireSessionSpeedChartFocus(") + """
const announce={textContent:''};global.document={getElementById:()=>announce};
const canvas={};const active=[],located=[];
function sessionSpeedPointDetails(row){return `rate=${row.rate};calls=${row.calls}`;}
function locateSessionSpeedResponse(row){located.push(row.rate);}
const chart={data:{datasets:[{data:[{y:12,speedRow:{rate:12,calls:1}},{y:null,speedRow:{}},{y:20,speedRow:{rate:20,calls:2}}]}]},
 setActiveElements(points){active.push(points);},getDatasetMeta(){return {data:[{x:1,y:2},{x:2,y:3},{x:3,y:4}]};},tooltip:{setActiveElements(){}},update(){}};
wireSessionSpeedChartFocus(canvas,chart);
const key=(key)=>canvas.onkeydown({key,preventDefault(){}});
key('ArrowRight');const first=announce.textContent;key('ArrowRight');const second=announce.textContent;key('Enter');key('Escape');
console.log(JSON.stringify({first,second,located,last:active.at(-1),dismissed:announce.textContent}));
"""
    result = run_js(tmp_path, program)
    assert result == {"first": "rate=12;calls=1", "second": "rate=20;calls=2", "located": [20], "last": [], "dismissed": ""}


def test_empty_session_uses_one_state_and_hides_table_controls_despite_flex_css():
    renderer = function("function renderSessionSpeedTimeline(")
    assert "empty.textContent = emptyMessage" in renderer
    assert "controls.style.display = selected ? 'flex' : 'none'" in renderer
    assert "if (!selected)" in renderer
    assert "document.getElementById('sessionSpeedChartView').hidden = true" in renderer
    assert "document.getElementById('sessionSpeedResponsesView').hidden = true" in renderer
    assert 'id="sessionSpeedRetry"' in SRC
    assert "loadSessionSpeedTimeline(sessionSpeedModalState.token, true, sessionSpeedModalState.requestedGroup)" in function("function initSessionSpeedModal(")


def test_modal_renderer_uses_ratio_summary_and_real_missing_rows_and_empty_states(tmp_path):
    program = STUBS + "\n".join(function(name) for name in [
        "function sessionSpeedRate(", "function sessionSpeedGroupKey(", "function buildSessionSpeedPlot(",
        "function sessionSpeedPointDetails(", "function sessionSpeedRecordedTime(", "function renderSessionSpeedTimeline(",
    ]) + """
function element(tag='div'){return {tag,style:{},dataset:{},parts:[],hidden:false,textContent:'',
 replaceChildren(...parts){this.parts=parts;},appendChild(child){this.parts.push(child);},setAttribute(){},getContext(){return {};}};}
const ids=['sessionSpeedPanel','sessionSpeedBody','sessionSpeedNote','sessionSpeedCoverage','sessionSpeedModel','sessionSpeedEmpty','sessionSpeedControls','sessionOutputSpeedChart','sessionSpeedChartView','sessionSpeedResponsesView','sessionSpeedRetry'];
const elements=Object.fromEntries(ids.map(id=>[id,element()]));
global.document={getElementById:id=>elements[id],createElement:element};
const sessionSpeedModalState={group:'',view:'chart'};let sessionOutputSpeedChart=null;const activeSessionDetail=null;
function formatTimeOnly(value){return String(value).slice(11,16);}function formatDuration(ms){return String(ms);}
function formatTokenCount(value){return String(value);}function usageReportSpeedFormatStamp(ms){return String(ms);}
function createTableCell(tag,classes,text){const cell=element(tag);cell.className=classes;cell.textContent=text;return cell;}
function getChartPalette(){return ['blue'];}function wireSessionSpeedChartFocus(){}function setSessionSpeedView(){}
function createChart(ctx,config){return {data:config.data,config,destroy(){}};}
const group={model:'a',measurement_kind:'response_window',token_basis:'output_including_reasoning',speed_calls:2,eligible_calls:3,coverage:2/3,output_tok_per_s:32.5};
const payload={cache:{state:'ready'},groups:[group],summary:{eligible_calls:3},series:[
 {...group,sequence:1,recorded_at_ms:10000,measurement_status:'measured',output_tok_per_s:100,speed_tokens:100,speed_ms:1000,speed_calls:1},
 {...group,sequence:2,recorded_at_ms:20000,measurement_status:'measured',output_tok_per_s:10,speed_tokens:30,speed_ms:3000,speed_calls:1},
 {model:'a',sequence:3,recorded_at_ms:null,measurement_status:'missing_timing',output_tok_per_s:null,speed_tokens:0,speed_calls:0}
]};
renderSessionSpeedTimeline(payload);
const measured={header:elements.sessionSpeedCoverage.textContent,rows:elements.sessionSpeedBody.parts.map(row=>row.parts.map(cell=>cell.textContent)),points:sessionOutputSpeedChart.data.datasets[0].data.map(point=>point.y),controls:elements.sessionSpeedControls.style.display};
renderSessionSpeedTimeline({...payload,cache:{state:'stale',job_id:null}});
const stale={note:elements.sessionSpeedNote.textContent,points:sessionOutputSpeedChart.data.datasets[0].data.map(point=>point.y)};
renderSessionSpeedTimeline({...payload,cache:{state:'stale',job_id:'active'}});
const buildingNote=elements.sessionSpeedNote.textContent;
renderSessionSpeedTimeline({cache:{state:'stale',job_id:null},summary:{},groups:[],series:[]});
const emptyStale=elements.sessionSpeedEmpty.textContent;
renderSessionSpeedTimeline({cache:{state:'ready'},summary:{measurement_status:'unsupported_reader'},groups:[],series:[]});
const empty={text:elements.sessionSpeedEmpty.textContent,header:elements.sessionSpeedCoverage.textContent,rows:elements.sessionSpeedBody.parts.length,chartHidden:elements.sessionSpeedChartView.hidden,tableHidden:elements.sessionSpeedResponsesView.hidden,controls:elements.sessionSpeedControls.style.display};
console.log(JSON.stringify({measured,stale,buildingNote,emptyStale,empty}));
"""
    result = run_js(tmp_path, program)
    assert result["measured"]["header"].startswith("32.5 ")
    assert result["measured"]["points"] == [100, 10, None]
    assert result["measured"]["rows"][2][2:] == ["—", "—", "—"]
    assert result["measured"]["controls"] == "flex"
    assert 'sessionSpeedStale' in result['stale']['note']
    assert 'sessionSpeedPendingHint' not in result['stale']['note'] and 'loading' not in result['stale']['note']
    assert result['stale']['points'] == [100, 10, None]
    assert 'sessionSpeedPendingHint' in result['buildingNote']
    assert result['emptyStale'] == 'sessionSpeedStale'
    assert result["empty"] == {"text": "sessionSpeedUnsupported", "header": "", "rows": 0, "chartHidden": True, "tableHidden": True, "controls": "none"}


def test_invalid_recorded_clocks_remain_absent_in_response_table(tmp_path):
    program = function("function sessionSpeedRecordedTime(") + """
function formatTimeOnly(value){return String(value).slice(11,16);}
console.log(JSON.stringify([null,'invalid',1e20,1791540000000].map(recorded_at_ms=>sessionSpeedRecordedTime({recorded_at_ms}))));
"""
    result = run_js(tmp_path, program)
    assert result[:3] == ["—", "—", "—"]
    assert result[3] != "—"


def test_changing_metric_group_retires_an_older_timeline_response(tmp_path):
    program = function("async function loadSessionSpeedTimeline(") + """
const sessionSpeedModalState={token:1,tool:'codex',sessionId:'session',server:{id:'one'},loading:false};
const pending=[],requests=[],painted=[];
function sessionSpeedModalCurrent(token){return token===sessionSpeedModalState.token;}
function renderSessionSpeedTimeline(payload){painted.push(payload.selected);}
async function fetchFromHost(server,path){requests.push(path);return new Promise(resolve=>pending.push(resolve));}
(async()=>{const a=loadSessionSpeedTimeline(1,false,JSON.stringify(['a','response_window','output_including_reasoning']));
 sessionSpeedModalState.loading=false;const b=loadSessionSpeedTimeline(1,false,JSON.stringify(['b','request_window','output_excluding_reasoning']));
 pending[0]({selected:'a',cache:{state:'ready'}});await a;
 pending[1]({selected:'b',cache:{state:'ready'}});await b;
 console.log(JSON.stringify({painted,requests}));})();
"""
    result = run_js(tmp_path, program)
    assert result["painted"] == ["b"]
    assert "model=a" in result["requests"][0]
    assert "model=b" in result["requests"][1]
    assert "measurement_kind=request_window" in result["requests"][1]
    assert "token_basis=output_excluding_reasoning" in result["requests"][1]


def test_large_timeline_requests_one_selected_group_and_labels_truncation(tmp_path):
    program = function("async function loadSessionSpeedTimeline(") + """
const sessionSpeedModalState={token:1,tool:'kimi',sessionId:'session',server:{id:'one'},loading:false,group:JSON.stringify(['model','server_decode','output_reasoning_unspecified'])};
const requests=[];function sessionSpeedModalCurrent(token){return token===1;}
function renderSessionSpeedTimeline(){}function scheduleSessionSpeedList(){}
async function fetchFromHost(server,path){requests.push(path);return {cache:{state:'ready'},total_responses:2000,truncated:true};}
(async()=>{await loadSessionSpeedTimeline(1);await new Promise(done=>setImmediate(done));console.log(JSON.stringify(requests));})();
"""
    result = run_js(tmp_path, program)
    assert len(result) == 2
    assert "&model=" not in result[0]
    assert "&model=model" in result[1]
    assert all("max_points=1000" in path for path in result)
    renderer = function("function renderSessionSpeedTimeline(")
    assert "sessionSpeedTruncated" in renderer
    assert "sessionSpeedNoPlotted" in renderer


def test_group_boundary_points_still_break_selected_stream_lines(tmp_path):
    program = "\n".join([function("function sessionSpeedRate("), function("function sessionSpeedGroupKey("), function("function buildSessionSpeedPlot(")]) + """
const group={model:'a',measurement_kind:'response_window',token_basis:'output_including_reasoning'};
const series=[{...group,sequence:1,recorded_at_ms:10000,measurement_status:'measured',output_tok_per_s:10},
 {...group,sequence:2,recorded_at_ms:20000,measurement_status:'group_boundary',output_tok_per_s:null},
 {...group,sequence:3,recorded_at_ms:30000,measurement_status:'measured',output_tok_per_s:20}];
console.log(JSON.stringify(buildSessionSpeedPlot(series,sessionSpeedGroupKey(group)).streams[0].points.map(point=>point.y)));
"""
    assert run_js(tmp_path, program) == [10, None, 20]


def test_hiding_analytics_stops_speed_without_retiring_core_session_identity(tmp_path):
    program = function("function sessionModalCurrent(") + function("function sessionSpeedModalCurrent(") + """
const sessionSpeedModalState={token:1};const modal={classList:{contains:()=>false}};const analytics={style:{display:'block'}};
global.document={hidden:false,getElementById:id=>id==='sessionModal'?modal:analytics};
const shown=[sessionModalCurrent(1),sessionSpeedModalCurrent(1)];analytics.style.display='none';
const chat=[sessionModalCurrent(1),sessionSpeedModalCurrent(1)];analytics.style.display='block';document.hidden=true;
console.log(JSON.stringify({shown,chat,hidden:sessionSpeedModalCurrent(1)}));
"""
    assert run_js(tmp_path, program) == {"shown": [True, True], "chat": [True, False], "hidden": False}
    switcher = function("function setSessionModalView(")
    assert "sessionSpeedModalState.controller?.abort()" in switcher
    assert "sessionSpeedModalState.stopJob?.()" in switcher


def test_physical_input_boundary_keeps_both_rates_and_breaks_the_segment(tmp_path):
    program = "\n".join([function("function sessionSpeedRate("), function("function sessionSpeedGroupKey("), function("function buildSessionSpeedPlot(")]) + """
const group={model:'a',measurement_kind:'response_window',token_basis:'output_including_reasoning'};
const rows=[{...group,stream_id:'main',sequence:1,recorded_at_ms:10000,measurement_status:'measured',output_tok_per_s:10},
 {...group,stream_id:'main',sequence:2,recorded_at_ms:20000,break_before:true,measurement_status:'measured',output_tok_per_s:20},
 {...group,stream_id:'main',sequence:3,recorded_at_ms:30000,measurement_status:'measured',output_tok_per_s:30}];
const plot=buildSessionSpeedPlot(rows,sessionSpeedGroupKey(group));
console.log(JSON.stringify(plot.streams[0].points.map(point=>({x:point.x,y:point.y,sequence:point.speedRow?.sequence??null}))));
"""
    assert run_js(tmp_path, program) == [
        {"x": 0, "y": 10, "sequence": 1},
        {"x": 10, "y": None, "sequence": None},
        {"x": 10, "y": 20, "sequence": 2},
        {"x": 20, "y": 30, "sequence": 3},
    ]


def test_explicit_tab_navigation_retires_list_work_without_a_dom_mutation_watcher(tmp_path):
    program = function("function sessionSpeedExplorerNavigation(") + """
const events=[];function scheduleSessionSpeedList(force){events.push(['load',force]);}function retireSessionSpeedList(){events.push(['retire']);}
sessionSpeedExplorerNavigation('overview');sessionSpeedExplorerNavigation('sessions');sessionSpeedExplorerNavigation('speed');
console.log(JSON.stringify(events));
"""
    assert run_js(tmp_path, program) == [["retire"], ["load", True], ["retire"]]
    assert "new MutationObserver" not in function("function initSessionSpeedExplorer(")
    assert "sessionSpeedExplorerNavigation(tab)" in function("function activateDashboardTab(")
