"""
ntqq_wrapped.py - 群聊时光机与年度/阶段总结生成器 (Chat Wrapped)
汇总长周期内群聊里的大事件、趣味之最、守夜人纪录与专属成就勋章。
兼容 dict 与 sqlite3.Row，且 HTML 视图具备严格的 XSS 转义防护。
"""

import html
from collections import Counter
from datetime import datetime


def _get_val(row, keys, default=None):
    """同时兼容 dict 和 sqlite3.Row，并支持字段别名回退。"""
    if isinstance(keys, str):
        keys = [keys]
    for k in keys:
        if isinstance(row, dict):
            if k in row and row[k] is not None:
                return row[k]
        else:
            try:
                val = row[k]
                if val is not None:
                    return val
            except (KeyError, IndexError, TypeError):
                pass
    return default


def build_chat_wrapped(messages: list, period_name: str = "年度时光机") -> dict:
    """构建时光机全景统计指标。兼容 dict 与 sqlite3.Row。"""
    if not messages:
        return {
            "period_name": period_name,
            "total_messages": 0,
            "unique_speakers": 0,
            "badges": [],
        }

    total_msgs = len(messages)
    speakers = Counter()
    hours = Counter()
    days = Counter()
    night_speakers = Counter()
    early_speakers = Counter()
    longest_text = ""
    longest_speaker = "无"
    
    for m in messages:
        sname = _get_val(m, ["sender_name", "name", "disp_name"])
        if not sname or sname in ("系统", "未知", "系统消息"):
            continue
        speakers[sname] += 1
        
        h = _get_val(m, ["hour"])
        if h is not None and h >= 0:
            hours[h] += 1
            if h in (0, 1, 2, 3, 4, 5):
                night_speakers[sname] += 1
            elif h in (6, 7, 8):
                early_speakers[sname] += 1
                
        d = _get_val(m, ["day"])
        if d:
            days[d] += 1
            
        txt = (_get_val(m, ["text", "msg_text"], "") or "").strip()
        if len(txt) > len(longest_text):
            longest_text = txt
            longest_speaker = sname

    unique_spks = len(speakers)
    top_speaker = speakers.most_common(1)[0][0] if speakers else "无"
    top_speaker_count = speakers.most_common(1)[0][1] if speakers else 0
    night_owl = night_speakers.most_common(1)[0][0] if night_speakers else "无"
    early_bird = early_speakers.most_common(1)[0][0] if early_speakers else "无"
    peak_day = days.most_common(1)[0][0] if days else "无"
    peak_day_count = days.most_common(1)[0][1] if days else 0

    badges = []
    if top_speaker != "无":
        badges.append({
            "title": "👑 群星之冠",
            "holder": top_speaker,
            "desc": f"累计发言 {top_speaker_count} 条，当之无愧的群内核心领袖",
        })
    if night_owl != "无":
        badges.append({
            "title": "🌙 深夜守夜人",
            "holder": night_owl,
            "desc": f"在凌晨 00:00~05:59 累计发送 {night_speakers[night_owl]} 条消息",
        })
    if early_bird != "无":
        badges.append({
            "title": "🌅 晨曦拂晓",
            "holder": early_bird,
            "desc": f"在清晨 06:00~08:59 累计发送 {early_speakers[early_bird]} 条消息",
        })
    if len(longest_text) >= 120:
        badges.append({
            "title": "📜 深度长文作者",
            "holder": longest_speaker,
            "desc": f"贡献了单条长达 {len(longest_text)} 字的深度剖析与锐评",
        })

    return {
        "period_name": period_name,
        "total_messages": total_msgs,
        "unique_speakers": unique_spks,
        "top_speaker": top_speaker,
        "top_speaker_count": top_speaker_count,
        "night_owl_speaker": night_owl,
        "early_bird_speaker": early_bird,
        "longest_speaker": longest_speaker,
        "longest_text_sample": (longest_text[:80] + "...") if len(longest_text) > 80 else longest_text,
        "peak_day": peak_day,
        "peak_day_count": peak_day_count,
        "speakers_top5": speakers.most_common(5),
        "badges": badges,
    }


def format_wrapped_markdown(w: dict) -> str:
    """渲染时光机 Markdown 报告。"""
    lines = [
        f"# ✨ {w['period_name']} · 群聊回忆录\n",
        f"> 在这段时光里，我们共同沉淀了 **{w['total_messages']}** 条消息，**{w['unique_speakers']}** 位群友曾在这里留下足迹。\n",
        "## 🏆 年度荣誉徽章\n",
    ]

    for b in w.get("badges", []):
        lines.append(f"- **{b['title']}** : **{b['holder']}** — _{b['desc']}_")
    lines.append("")

    lines.append("## 📊 趣味数字盘点\n")
    lines.append(f"- 🔥 **最热闹的一天**：`{w['peak_day']}`，单日狂轰 **{w['peak_day_count']}** 条消息")
    lines.append(f"- 🌙 **深夜守夜人**：`{w['night_owl_speaker']}` 陪伴群聊度过寂静长夜")
    lines.append(f"- 🌅 **晨曦拂晓者**：`{w['early_bird_speaker']}` 总是最早开启群内新一天")
    lines.append(f"- 📝 **长文锐评王**：`{w['longest_speaker']}`（代表作：_{w.get('longest_text_sample', '')}_）")
    lines.append("")

    if w.get("speakers_top5"):
        lines.append("## 🌟 发言风云榜 Top 5\n")
        for idx, (spk, cnt) in enumerate(w["speakers_top5"], 1):
            lines.append(f"{idx}. **{spk}** — `{cnt} 条消息`")
        lines.append("")

    return "\n".join(lines)


def render_wrapped_html(w: dict) -> str:
    """渲染交互式时光机单文件 HTML 卡片画廊（包含完整 XSS 转义）。"""
    esc = html.escape
    title = esc(str(w.get("period_name") or "群聊时光机"))
    top_spk = esc(str(w.get("top_speaker") or "无"))
    night_spk = esc(str(w.get("night_owl_speaker") or "无"))

    badge_cards = []
    for b in w.get("badges", []):
        b_title = esc(str(b.get("title", "")))
        b_holder = esc(str(b.get("holder", "")))
        b_desc = esc(str(b.get("desc", "")))
        badge_cards.append(
            f'<div class="badge-card"><div class="badge-title">{b_title} · {b_holder}</div><div class="badge-desc">{b_desc}</div></div>'
        )
    badge_html = "".join(badge_cards)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{title} - QQ群聊时光机</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: rgba(30, 41, 59, 0.7);
      --accent: #f59e0b;
      --text: #f8fafc;
      --sub: #94a3b8;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at 50% 20%, #1e1b4b 0%, #0f172a 100%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px;
      box-sizing: border-box;
    }}
    .wrapped-card {{
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 24px;
      max-width: 680px;
      width: 100%;
      padding: 36px;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
    }}
    h1 {{
      font-size: 28px;
      margin-top: 0;
      background: linear-gradient(135deg, #f59e0b, #ec4899);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 16px;
      margin: 24px 0;
    }}
    .stat-box {{
      background: rgba(15, 23, 42, 0.6);
      border-radius: 16px;
      padding: 16px;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }}
    .stat-val {{
      font-size: 24px;
      font-weight: 700;
      color: var(--accent);
    }}
    .stat-lbl {{
      font-size: 13px;
      color: var(--sub);
      margin-top: 4px;
    }}
    .badge-card {{
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.1), rgba(236, 72, 153, 0.1));
      border: 1px solid rgba(245, 158, 11, 0.3);
      border-radius: 12px;
      padding: 12px 16px;
      margin-bottom: 12px;
    }}
    .badge-title {{
      font-weight: 700;
      color: #fbbf24;
    }}
    .badge-desc {{
      font-size: 13px;
      color: var(--sub);
      margin-top: 4px;
    }}
  </style>
</head>
<body>
  <div class="wrapped-card">
    <h1>✨ {title}</h1>
    <p style="color:var(--sub)">群聊回忆录 · 共同度过的每一个高光瞬间</p>
    <div class="stat-grid">
      <div class="stat-box">
        <div class="stat-val">{w.get('total_messages', 0)}</div>
        <div class="stat-lbl">条消息记录</div>
      </div>
      <div class="stat-box">
        <div class="stat-val">{w.get('unique_speakers', 0)}</div>
        <div class="stat-lbl">位活跃群友</div>
      </div>
      <div class="stat-box">
        <div class="stat-val">{top_spk}</div>
        <div class="stat-lbl">👑 核心发言担当</div>
      </div>
      <div class="stat-box">
        <div class="stat-val">{night_spk}</div>
        <div class="stat-lbl">🌙 深夜守夜人</div>
      </div>
    </div>
    <h3 style="margin-top:24px;font-size:18px">🏆 荣誉勋章</h3>
    {badge_html}
  </div>
</body>
</html>"""
