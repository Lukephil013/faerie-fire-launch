# One-shot port: pull the Korean translation layer out of the Korean copy and
# graft it into this (unified) copy's memory.html, gated behind a language
# setting chosen at first boot. Run once via "RUN PORT.bat"; safe to re-run
# (it refuses if already ported). Writes port_result.txt.
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KO_PATH = os.path.join(HERE, "..", "Faerie Fire Korean", "livingpc", "ui", "memory.html")
EN_PATH = os.path.join(HERE, "livingpc", "ui", "memory.html")


def main() -> str:
    ko = open(KO_PATH, encoding="utf-8").read()
    en = open(EN_PATH, encoding="utf-8").read()

    assert ko.rstrip().endswith("</html>"), "korean file truncated?"
    assert en.rstrip().endswith("</html>"), "english file truncated?"
    assert "KO_TERM_SUBS" in ko, "korean file missing KO_TERM_SUBS"
    assert "pollBridge" in en, "english file missing boot fix"
    if "const KO_UI_TEXT" in en:
        return "already ported — nothing to do"

    # backup
    open(EN_PATH + ".pre-port.bak", "w", encoding="utf-8").write(en)

    # ---- extract KO block ----
    start = ko.index("const KO_UI_TEXT")
    observer_start = ko.index(
        "new MutationObserver(records=>scheduleKoreanLocalization(records)).observe", start)
    tail_marker = "scheduleKoreanLocalization();"
    tail_end = ko.index(tail_marker, observer_start) + len(tail_marker)
    block = ko[start:tail_end]

    # ---- gate activation behind enableKoreanUI() ----
    old_tail = block[block.index(
        "new MutationObserver(records=>scheduleKoreanLocalization(records)).observe"):]
    new_tail = (
        "let koObserverStarted=false;\n"
        "function enableKoreanUI(){\n"
        "  if(koObserverStarted) return;\n"
        "  koObserverStarted=true;\n"
        "  APP_LANG='ko';\n"
        "  document.documentElement.lang='ko';\n"
        "  new MutationObserver(records=>scheduleKoreanLocalization(records)).observe(document.body,{\n"
        "    childList:true, subtree:true, characterData:true, attributes:true,\n"
        "    attributeFilter:['placeholder','title','aria-label']\n"
        "  });\n"
        "  scheduleKoreanLocalization();\n"
        "}"
    )
    block = block.replace(old_tail, new_tail)

    # ---- extra KO_UI_TEXT keys (metric panel context line) ----
    anchor = "'inquiry not found':'문의를 찾을 수 없어요',"
    assert anchor in block, "anchor key missing in KO_UI_TEXT"
    extra = anchor + "\n\n  // --- unified build: metric panel context line ---\n" + \
        "  'For this investigation:':'이 탐구에 대한 제안:',\n" + \
        "  '. These are the standard exercise starter measures, keyword-matched to this investigation — not written specifically for it. Rename or reword anything, or set importance to 0 to drop a row, before approving.':'. 표준 운동 시작 측정 항목이에요 — 이 탐구에 맞춰 새로 만든 것이 아니라 키워드로 매칭됐어요. 승인 전에 이름과 문구를 바꾸거나, 중요도를 0으로 두면 그 항목은 제외돼요.',\n" + \
        "  '. These are the standard mental-health starter measures, keyword-matched to this investigation — not written specifically for it. Rename or reword anything, or set importance to 0 to drop a row, before approving.':'. 표준 마음 건강 시작 측정 항목이에요 — 이 탐구에 맞춰 새로 만든 것이 아니라 키워드로 매칭됐어요. 승인 전에 이름과 문구를 바꾸거나, 중요도를 0으로 두면 그 항목은 제외돼요.',\n" + \
        "  '. These are the standard general starter measures, keyword-matched to this investigation — not written specifically for it. Rename or reword anything, or set importance to 0 to drop a row, before approving.':'. 표준 일반 시작 측정 항목이에요 — 이 탐구에 맞춰 새로 만든 것이 아니라 키워드로 매칭됐어요. 승인 전에 이름과 문구를 바꾸거나, 중요도를 0으로 두면 그 항목은 제외돼요.',\n" + \
        "  '. GoalAI drafted these measures from this investigation\\'s own framing — review, reword, or re-weight anything before approving.':'. GoalAI가 이 탐구의 맥락에서 직접 만든 측정 항목이에요 — 승인 전에 자유롭게 검토하고 수정하세요.',"
    block = block.replace(anchor, extra)

    # ---- insert into EN after 'use strict'; ----
    strict = "'use strict';"
    i = en.index(strict) + len(strict)
    en = en[:i] + "\nlet APP_LANG='en', appLanguageSet=true;\n" + block + "\n" + en[i:]

    # ---- wire bootstrap ----
    m = re.search(r"function applyAppBootstrap\((\w+)\)\{", en)
    assert m, "applyAppBootstrap not found"
    arg = m.group(1)
    inject = (f"\n  if({arg}&&{arg}.language==='ko') enableKoreanUI();"
              f"\n  if({arg}) appLanguageSet=!!{arg}.language_set;")
    en = en[:m.end()] + inject + en[m.end():]

    # ---- onboarding dots + language step ----
    dots_old = ('<span class="on" data-step="key"></span>'
                '<span data-step="soul"></span><span data-step="done"></span>')
    dots_new = ('<span class="on" data-step="lang"></span><span data-step="key"></span>'
                '<span data-step="soul"></span><span data-step="done"></span>')
    assert dots_old in en, "onboard dots not found"
    en = en.replace(dots_old, dots_new)

    step_key = '    <div class="onboard-step" id="onboard-step-key">'
    lang_step = (
        '    <div class="onboard-step" id="onboard-step-lang">\n'
        '      <div class="onboard-step-label">Language · 언어</div>\n'
        '      <h1>Choose your language · 언어를 선택하세요</h1>\n'
        '      <p class="onboard-copy">Faerie Fire will use this language everywhere — the interface and '
        "Faerie's own replies.<br>페어리 파이어의 화면과 페어리의 대답이 모두 이 언어로 표시돼요.</p>\n"
        '      <div class="onboard-actions">\n'
        '        <button class="accent" id="onboard-lang-en" type="button">English</button>\n'
        '        <button class="accent" id="onboard-lang-ko" type="button">한국어</button>\n'
        '      </div>\n'
        '    </div>\n\n'
    )
    assert step_key in en, "key step not found"
    en = en.replace(step_key, lang_step + step_key, 1)

    en = en.replace("  ['key','soul','done'].forEach(s=>{",
                    "  ['lang','key','soul','done'].forEach(s=>{", 1)

    open_old = (
        "  const focusStep=step=>{\n"
        "    onboardShowStep(step);\n"
        "    const focusId=step==='soul'?'onboard-soul-title':'onboard-key-input';\n"
        "    setTimeout(()=>{ const el=$(focusId); if(el) el.focus(); }, 50);\n"
        "  };\n"
        "  if(api()&&pywebview.api.onboarding_status){"
    )
    open_new = (
        "  const focusStep=step=>{\n"
        "    onboardShowStep(step);\n"
        "    const focusId=step==='soul'?'onboard-soul-title':'onboard-key-input';\n"
        "    setTimeout(()=>{ const el=$(focusId); if(el) el.focus(); }, 50);\n"
        "  };\n"
        "  // Unified build: ask for the language first if it was never chosen.\n"
        "  if(!appLanguageSet){ onboardShowStep('lang'); return; }\n"
        "  if(api()&&pywebview.api.onboarding_status){"
    )
    assert open_old in en, "openOnboarding body not found"
    en = en.replace(open_old, open_new, 1)

    handlers_old = "(function initOnboardingHandlers(){\n  const keyBtn=$('onboard-key-continue');"
    handlers_new = (
        "(function initOnboardingHandlers(){\n"
        "  const pickLang=lang=>{\n"
        "    if(api()&&pywebview.api.app_set_language) pywebview.api.app_set_language(lang).catch(()=>{});\n"
        "    appLanguageSet=true;\n"
        "    if(lang==='ko') enableKoreanUI();\n"
        "    const focusStep=step=>{\n"
        "      onboardShowStep(step);\n"
        "      const focusId=step==='soul'?'onboard-soul-title':'onboard-key-input';\n"
        "      setTimeout(()=>{ const el=$(focusId); if(el) el.focus(); }, 50);\n"
        "    };\n"
        "    if(api()&&pywebview.api.onboarding_status){\n"
        "      bridgeTimeout(pywebview.api.onboarding_status(),'Onboarding status',8000)\n"
        "        .then(r=>focusStep(r&&r.has_key?'soul':'key'))\n"
        "        .catch(()=>focusStep('key'));\n"
        "    } else { focusStep('key'); }\n"
        "  };\n"
        "  const langEn=$('onboard-lang-en');\n"
        "  if(langEn) langEn.onclick=()=>pickLang('en');\n"
        "  const langKo=$('onboard-lang-ko');\n"
        "  if(langKo) langKo.onclick=()=>pickLang('ko');\n"
        "  const keyBtn=$('onboard-key-continue');"
    )
    assert handlers_old in en, "onboarding handlers not found"
    en = en.replace(handlers_old, handlers_new, 1)

    open(EN_PATH, "w", encoding="utf-8").write(en)
    return f"ported OK — new size {len(en)} bytes (backup: memory.html.pre-port.bak)"


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:  # noqa: BLE001
        import traceback
        result = "FAILED: " + traceback.format_exc()
    with open(os.path.join(HERE, "port_result.txt"), "w", encoding="utf-8") as handle:
        handle.write(result + "\n")
    print(result)
