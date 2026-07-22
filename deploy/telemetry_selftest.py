"""隔离自测：驱动真实 telemetry 模块，多线程模拟真实时序，验证 2026-07 注册表重构。

新模型（按 turn_key=sentence_id 索引的注册表 + pending turn + root Context 隔离）：
- start_turn_span(conn, turn_id) -> 单个 span，存为 pending（root Context，不继承 implicit）
- bind_turn(conn, turn_key)      -> 把 pending 迁入注册表，绑定到本轮 sentence_id
- child_span(conn, name, attrs, turn_key=None) -> 按 turn_key 显式查父；空则回退 pending
- turn_span(conn, turn_key=None) / end_turn_span(conn, turn_key=None, end_reason=...)

覆盖场景（后两个正是用户报告的 bug 复现 + 修复证明）：
1  正常轮 + 拖尾 tts（孤儿修复：turn end 后迟到子 span 仍挂本轮）
2  被打断轮（llm+tts wasted=abort，turn.aborted）
3  空识别（asr span 标 wasted=empty，口径与 llm/tts 一致）
3b 识别出内容但恰逢打断（asr wasted=abort）
4  stale tts（旧轮残留）
5  ★跨轮交错：turn-A 已 end 后，A 的迟到 tts 与 turn-B 并存，必须挂回 A 不串到 B
6  ★跨 session 隔离：两连接并发，各自独立 trace（trace_id 不同），子 span 不跨会话
7  禁用态全链路 no-op
"""
import os, sys, threading, time

# 本文件位于 <repo>/deploy/，被测代码在 <repo>/main/xiaozhi-server。
# 用相对 __file__ 定位，避免硬编码绝对路径（换机器/换 clone 仍可跑）。
_SERVER_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "main", "xiaozhi-server")
)
sys.path.insert(0, _SERVER_ROOT)

os.environ["TELEMETRY_ENABLED"] = "true"
os.environ["TELEMETRY_CAPTURE_CONTENT"] = "true"

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core.utils import telemetry

# 用 InMemory exporter 接管，而非真去连 jaeger
exporter = InMemorySpanExporter()
provider = TracerProvider(resource=Resource.create({"service.name": "selftest"}))
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
telemetry._TRACER = trace.get_tracer("selftest")
telemetry._ENABLED = True
telemetry._CAPTURE_CONTENT = True


class Conn:
    def __init__(self, session_id="sess-1"):
        self.session_id = session_id
        self.device_id = "dev-1"
        self.sentence_id = None  # 起点为空，chat() depth==0 才生成
        self.client_abort = False


def _base_name(s):
    # wasted span 的 operation name 会被追加「·wasted:<reason>」后缀，
    # 这里按 base name 归一，便于沿用原有 spans_by_name 查找逻辑。
    return s.name.split("·wasted:", 1)[0]


def spans_by_name(name):
    return [s for s in exporter.get_finished_spans() if _base_name(s) == name]


def parent_of(span):
    # 返回 parent span_id，无 parent 返回 None
    return span.parent.span_id if span.parent else None


results = []
def check(cond, label):
    results.append((cond, label))
    print(("PASS " if cond else "FAIL ") + label)


# ---------- 场景1：正常轮 + 拖尾 tts（核心孤儿复现）----------
# 真实时序：ASR 起点建 pending → chat() bind sentence_id → llm(executor) → turn end
#          → 守护线程仍产出迟到 tts，必须仍挂本轮（不成孤儿）
conn = Conn()
telemetry.start_turn_span(conn, conn.session_id)  # pending turn（root ctx）
# asr 子 span 在 bind 之前：turn_key=None 回退 pending
with telemetry.child_span(conn, "asr", {"asr.text": "你好呀"}) as s:
    telemetry.set_attr(s, "asr.duration_ms", 10.0)
# chat() depth==0：生成 sentence_id 并 bind
conn.sentence_id = "turn-A"
telemetry.bind_turn(conn, "turn-A")
telemetry.record_user_input(conn, "你好呀", turn_key="turn-A")

def llm_thread():
    telemetry.stash_llm_usage({"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 64})
    with telemetry.child_span(conn, "llm", {"gen_ai.request.model": "deepseek"}, turn_key="turn-A") as s:
        u = telemetry.pop_llm_usage()
        telemetry.set_attr(s, "gen_ai.usage.cached_tokens", u["cached_tokens"])
        time.sleep(0.05)
t = threading.Thread(target=llm_thread); t.start(); t.join()

# turn 结束（发送线程判定播放完毕）
telemetry.end_turn_span(conn, turn_key="turn-A", end_reason="completed")

# 拖尾 tts：turn 已 end，但守护线程仍在合成迟到句，用同一 turn_key 显式挂父
def late_tts_thread():
    with telemetry.child_span(conn, "tts", {"tts.text_len": 5}, turn_key="turn-A") as s:
        telemetry.set_attr(s, "tts.text", "拜拜啦")
        time.sleep(0.03)
t = threading.Thread(target=late_tts_thread); t.start(); t.join()

provider.force_flush()
turnA = [s for s in spans_by_name("conversation.turn") if s.attributes.get("turn.id") == "sess-1"][0]
tidA = turnA.context.span_id
children = spans_by_name("asr") + spans_by_name("llm") + spans_by_name("tts")
check(all(parent_of(c) == tidA for c in children),
      f"场景1 孤儿修复：{len(children)}个子span全部挂在turn-A下(无孤儿)")
check(parent_of(turnA) is None, "场景1 turn 是独立 trace root（parent 为空）")
check(turnA.attributes.get("turn.end_reason") == "completed", "场景1 turn.end_reason=completed")
check(turnA.attributes.get("user.input") == "你好呀", "场景1 user.input 记录")
check(spans_by_name("llm")[0].attributes.get("gen_ai.usage.cached_tokens") == 64, "场景1 cached_tokens=64 桥接成功")
check(spans_by_name("tts")[0].attributes.get("tts.text") == "拜拜啦", "场景1 tts.text 记录")

exporter.clear()

# ---------- 场景2：被打断轮（llm+tts wasted）----------
conn = Conn()
telemetry.start_turn_span(conn, conn.session_id)
conn.sentence_id = "turn-B"; telemetry.bind_turn(conn, "turn-B")
with telemetry.child_span(conn, "asr", {"asr.text": "讲个笑话"}, turn_key="turn-B") as s: pass
with telemetry.child_span(conn, "llm", turn_key="turn-B") as s:
    telemetry.mark_wasted(s, "abort")
conn.client_abort = True
def tts_abort_thread():
    with telemetry.child_span(conn, "tts", {"tts.text_len": 3}, turn_key="turn-B") as s:
        telemetry.set_attr(s, "tts.text", "从前")
        if conn.client_abort:
            telemetry.mark_wasted(s, "abort")
t = threading.Thread(target=tts_abort_thread); t.start(); t.join()
telemetry.end_turn_span(conn, turn_key="turn-B", end_reason="aborted")
provider.force_flush()
turnB = [s for s in spans_by_name("conversation.turn")][0]
check(turnB.attributes.get("turn.aborted") is True, "场景2 turn.aborted=True")
check(spans_by_name("llm")[0].attributes.get("wasted") is True, "场景2 llm.wasted=True")
check(spans_by_name("tts")[0].attributes.get("waste.reason") == "abort", "场景2 tts.waste_reason=abort")
# wasted span 的 operation name 被追加「·wasted:<reason>」后缀，供 Jaeger 时间轴肉眼可辨
check(spans_by_name("llm")[0].name == "llm·wasted:abort", "场景2 llm span 改名 llm·wasted:abort")
check(spans_by_name("tts")[0].name == "tts·wasted:abort", "场景2 tts span 改名 tts·wasted:abort")
exporter.clear()

# ---------- 场景3：空识别（asr span 标 wasted=empty）----------
conn = Conn()
telemetry.start_turn_span(conn, conn.session_id)  # pending（空识别不会 bind sentence_id）
# 新 ASR 流程：asr span 手动 enter/exit，拿到 text_len==0 后标 wasted 再收尾
asr_cm = telemetry.child_span(conn, "asr"); asr_s = asr_cm.__enter__()
telemetry.set_attr(asr_s, "asr.text", "")
telemetry.mark_wasted(asr_s, "empty")
asr_cm.__exit__(None, None, None)
# turn 尚未 bind，turn_span(conn) 回退到 pending
telemetry.set_attr(telemetry.turn_span(conn), "turn.empty_asr", True)
telemetry.end_turn_span(conn, end_reason="empty")  # turn_key 空 → 结束 pending
provider.force_flush()
turnC = [s for s in spans_by_name("conversation.turn")][0]
check(turnC.attributes.get("turn.end_reason") == "empty", "场景3 turn.end_reason=empty")
check(turnC.attributes.get("turn.empty_asr") is True, "场景3 turn.empty_asr=True")
check(spans_by_name("asr")[0].attributes.get("waste.reason") == "empty", "场景3 asr.waste_reason=empty")
check(parent_of(spans_by_name("asr")[0]) == turnC.context.span_id, "场景3 asr 挂在 pending turn 下")
exporter.clear()

# ---------- 场景3b：识别出内容但恰逢打断态（asr span 标 wasted=abort）----------
conn = Conn(); conn.client_abort = True
telemetry.start_turn_span(conn, conn.session_id)
asr_cm = telemetry.child_span(conn, "asr"); asr_s = asr_cm.__enter__()
telemetry.set_attr(asr_s, "asr.text", "还在么")
if getattr(conn, "client_abort", False):
    telemetry.mark_wasted(asr_s, "abort")
asr_cm.__exit__(None, None, None)
telemetry.end_turn_span(conn, end_reason="aborted")
provider.force_flush()
check(spans_by_name("asr")[0].attributes.get("waste.reason") == "abort", "场景3b asr.waste_reason=abort")
exporter.clear()

# ---------- 场景4：stale tts（旧轮残留）----------
conn = Conn()
turnD_span = telemetry.start_turn_span(conn, conn.session_id)
turnD_id = turnD_span.get_span_context().span_id
conn.sentence_id = "turn-D"; telemetry.bind_turn(conn, "turn-D")
# 模拟：tts 合成时其 current_sentence_id 已落后于 conn.sentence_id
class TtsLike: pass
tl = TtsLike(); tl.current_sentence_id = "turn-D"; conn.sentence_id = "turn-E-new"
with telemetry.child_span(conn, "tts", {"tts.text_len": 4}, turn_key=tl.current_sentence_id) as s:
    if tl.current_sentence_id and conn.sentence_id and tl.current_sentence_id != conn.sentence_id:
        telemetry.mark_wasted(s, "stale")
telemetry.end_turn_span(conn, turn_key="turn-D", end_reason="completed")
provider.force_flush()
check(spans_by_name("tts")[0].attributes.get("waste.reason") == "stale", "场景4 tts.waste_reason=stale")
# stale tts 仍应挂在它真正所属的 turn-D 下（用 turn_key 显式查父）
check(parent_of(spans_by_name("tts")[0]) == turnD_id, "场景4 stale tts 挂在 turn-D 下(非孤儿/非错轮)")
exporter.clear()

# ---------- 场景5：★跨轮交错（复现"时序混乱"并证明修复）----------
# turn-A 完整结束后，turn-B 起；A 的迟到 tts 与 B 并存，必须挂回 A，绝不串到 B
conn = Conn()
# 轮 A
telemetry.start_turn_span(conn, conn.session_id)
conn.sentence_id = "turn-A5"; telemetry.bind_turn(conn, "turn-A5")
with telemetry.child_span(conn, "llm", turn_key="turn-A5") as s:
    telemetry.set_attr(s, "gen_ai.request.model", "A-llm")
telemetry.end_turn_span(conn, turn_key="turn-A5", end_reason="completed")
# 轮 B（新一轮起点：新 pending → 新 sentence_id）
telemetry.start_turn_span(conn, conn.session_id)
conn.sentence_id = "turn-B5"; telemetry.bind_turn(conn, "turn-B5")
# 交错：A 的迟到 tts 现在才到（B 已是当前轮），用 turn_key=A5 必须挂回 A
def a_late_tts():
    with telemetry.child_span(conn, "tts", {"tts.text": "A迟到"}, turn_key="turn-A5") as s:
        telemetry.set_attr(s, "tts.text", "A迟到")
def b_tts():
    with telemetry.child_span(conn, "tts", {"tts.text": "B正常"}, turn_key="turn-B5") as s:
        telemetry.set_attr(s, "tts.text", "B正常")
ta = threading.Thread(target=a_late_tts); tb = threading.Thread(target=b_tts)
ta.start(); tb.start(); ta.join(); tb.join()
telemetry.end_turn_span(conn, turn_key="turn-B5", end_reason="completed")
provider.force_flush()
turn_spans = spans_by_name("conversation.turn")
# 用 tts 的父来校验交错正确：A 轮 turn = llm 挂靠的 turn
tts_a = [s for s in spans_by_name("tts") if s.attributes.get("tts.text") == "A迟到"][0]
tts_b = [s for s in spans_by_name("tts") if s.attributes.get("tts.text") == "B正常"][0]
llm_a = spans_by_name("llm")[0]
turnA5_id = parent_of(llm_a)          # A 轮 turn 的 span_id（llm 挂在 A 下）
# B 轮 turn = 与 A 不同的那个
b_candidates = [ts.context.span_id for ts in turn_spans if ts.context.span_id != turnA5_id]
turnB5_id = b_candidates[0] if b_candidates else None
check(parent_of(tts_a) == turnA5_id, "场景5 A的迟到tts挂回turn-A(未串到B)")
check(parent_of(tts_b) == turnB5_id, "场景5 B的tts挂在turn-B")
check(turnA5_id != turnB5_id, "场景5 两轮是不同 turn span")
# 两轮各自独立 trace（时序不会合并成一个巨型 turn）
trace_ids = {ts.context.trace_id for ts in turn_spans}
check(len(trace_ids) == 2, "场景5 两轮分属两个独立 trace（无跨轮合并）")
exporter.clear()

# ---------- 场景6：★跨 session 隔离（复现"混合多个 sessionid"并证明修复）----------
# 两连接并发跑，各自 turn 必须是独立 trace，子 span 不跨会话，trace_id 不同
c1 = Conn(session_id="sess-A"); c2 = Conn(session_id="sess-B")
barrier = threading.Barrier(2)
def run_session(conn, key, txt):
    barrier.wait()  # 尽量让两会话在同一时刻交错，逼出 implicit context 串扰
    telemetry.start_turn_span(conn, conn.session_id)
    conn.sentence_id = key; telemetry.bind_turn(conn, key)
    with telemetry.child_span(conn, "asr", {"asr.text": txt}, turn_key=key): pass
    with telemetry.child_span(conn, "llm", {"gen_ai.request.model": txt}, turn_key=key): pass
    with telemetry.child_span(conn, "tts", {"tts.text": txt}, turn_key=key) as s:
        telemetry.set_attr(s, "tts.text", txt)
    telemetry.end_turn_span(conn, turn_key=key, end_reason="completed")
t1 = threading.Thread(target=run_session, args=(c1, "s1-t1", "会话A"))
t2 = threading.Thread(target=run_session, args=(c2, "s2-t1", "会话B"))
t1.start(); t2.start(); t1.join(); t2.join()
provider.force_flush()
turns = spans_by_name("conversation.turn")
turnSA = [s for s in turns if s.attributes.get("session.id") == "sess-A"][0]
turnSB = [s for s in turns if s.attributes.get("session.id") == "sess-B"][0]
check(turnSA.context.trace_id != turnSB.context.trace_id, "场景6 两 session 各自独立 trace_id(未混合)")
# 会话A 的三个子 span 全部挂在 turnSA 下，且 trace_id == turnSA
a_children = [s for s in spans_by_name("asr") + spans_by_name("llm") + spans_by_name("tts")
              if s.attributes.get("asr.text") == "会话A"
              or s.attributes.get("gen_ai.request.model") == "会话A"
              or s.attributes.get("tts.text") == "会话A"]
check(len(a_children) == 3 and all(parent_of(c) == turnSA.context.span_id for c in a_children),
      "场景6 会话A的3个子span全挂 sess-A 下")
check(all(c.context.trace_id == turnSA.context.trace_id for c in a_children),
      "场景6 会话A子span trace_id 与 sess-A turn 一致")
b_children = [s for s in spans_by_name("asr") + spans_by_name("llm") + spans_by_name("tts")
              if s.attributes.get("asr.text") == "会话B"
              or s.attributes.get("gen_ai.request.model") == "会话B"
              or s.attributes.get("tts.text") == "会话B"]
check(len(b_children) == 3 and all(parent_of(c) == turnSB.context.span_id for c in b_children),
      "场景6 会话B的3个子span全挂 sess-B 下")
exporter.clear()

# ---------- 场景7：★系统主动发起、绕过 ASR（复现「找不到LLM的孤儿tts」bug）----------
# 空闲告别/超字数提示走 startToChat→chat() 但没经过 ASR，故无 pending turn。
# bind_turn 必须兜底新建 turn，让本轮 llm/tts 挂上父，不再是孤儿。
conn = Conn()
# 注意：没有 start_turn_span（未走 ASR）
conn.sentence_id = "sys-goodbye"
telemetry.bind_turn(conn, "sys-goodbye")  # 无 pending → 兜底新建
telemetry.record_user_input(conn, "请以依依不舍的话结束对话", turn_key="sys-goodbye")
def sys_llm():
    with telemetry.child_span(conn, "llm", {"gen_ai.completion": "谢谢你今天陪我"}, turn_key="sys-goodbye") as s:
        telemetry.set_attr(s, "gen_ai.completion", "谢谢你今天陪我")
def sys_tts():
    with telemetry.child_span(conn, "tts", {"tts.text": "拜拜~下次再聊"}, turn_key="sys-goodbye") as s:
        telemetry.set_attr(s, "tts.text", "拜拜~下次再聊")
tl = threading.Thread(target=sys_llm); tt = threading.Thread(target=sys_tts)
tl.start(); tl.join(); tt.start(); tt.join()
telemetry.end_turn_span(conn, turn_key="sys-goodbye", end_reason="completed")
provider.force_flush()
turnSys = [s for s in spans_by_name("conversation.turn") if s.attributes.get("turn.id") == "sys-goodbye"][0]
sys_children = spans_by_name("llm") + spans_by_name("tts")
check(len(sys_children) == 2 and all(parent_of(c) == turnSys.context.span_id for c in sys_children),
      "场景7 系统告别：无ASR也建turn，llm/tts全挂其下(非孤儿)")
check(all(c.context.trace_id == turnSys.context.trace_id for c in sys_children),
      "场景7 系统告别：llm/tts 与 turn 同一 trace(可从turn找到LLM)")
exporter.clear()

# ---------- 场景8：禁用态 no-op 安全 ----------
telemetry._ENABLED = False
c = Conn()
sp = telemetry.start_turn_span(c, "x")
telemetry.bind_turn(c, "x")
telemetry.record_user_input(c, "x", turn_key="x")
with telemetry.child_span(c, "tts", turn_key="x") as s:
    telemetry.mark_wasted(s, "abort")
telemetry.end_turn_span(c, turn_key="x", end_reason="completed")
check(sp is None, "场景8 禁用态：start_turn_span 返回 None，全链路 no-op 无异常")

print("\n==== SUMMARY ====")
passed = sum(1 for c, _ in results if c)
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
