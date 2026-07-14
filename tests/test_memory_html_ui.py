"""Fast checks for memory.html UI behavior without launching pywebview.

These tests intentionally avoid a browser dependency.  They cover the fragile
parts of the inline Growth UI script that are easy to regress while editing the
single large HTML file:

- the embedded JavaScript must parse;
- submitted Growth focus questions are filtered from future cards;
- the submit flow replaces answered boxes with pending/loading states before
  GoalAI refreshes follow-up questions.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "livingpc" / "ui" / "memory.html"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _scripts() -> list[str]:
    scripts = re.findall(r"<script>\s*(.*?)\s*</script>", _html(), re.DOTALL)
    assert scripts, "memory.html should contain inline script blocks"
    return scripts


def _script() -> str:
    """The application block, rather than the small early crash banner."""
    return max(_scripts(), key=len)


def _function_body(script: str, name: str) -> str:
    start_match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", script)
    assert start_match, f"{name}() not found"
    start = start_match.end()
    depth = 1
    idx = start
    while idx < len(script) and depth:
        char = script[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"{name}() body did not close"
    return script[start:idx - 1]


def test_memory_html_inline_script_parses_with_node():
    node = shutil.which("node")
    if not node:
        return
    for script in _scripts():
        result = subprocess.run(
            [node, "--check", "-"], input=script, text=True, encoding="utf-8",
            cwd=ROOT, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_growth_focus_questions_filter_already_submitted_answers():
    script = _script()
    answered = _function_body(script, "goalAnsweredQuestionKeys")
    evidence_questions = _function_body(script, "goalEvidenceQuestions")

    assert "ev.source_kind!=='focus_answer'" in answered
    assert "match(/^Question:" in answered
    assert "goalFocusPending" in answered
    assert "goalQuestionKey(match[1])" in answered

    assert "goalAnsweredQuestionKeys(node)" in evidence_questions
    assert ".filter(q=>!answered.has(goalQuestionKey(q.question)))" in evidence_questions


def test_submit_answers_shows_pending_state_before_goalai_review():
    body = _function_body(_script(), "submitGoalFocusAnswers")

    first_pending = body.find("title:'Saving answers…'")
    first_render = body.find("renderGoalFocusPanel();", first_pending)
    first_api = body.find("pywebview.api.goal_add_evidence")
    assert -1 not in (first_pending, first_render, first_api)
    assert first_pending < first_render < first_api

    review_pending = body.find("title:'Generating follow-up questions…'")
    review_call = body.find("pywebview.api.goal_ai_review")
    reload_call = body.find("pywebview.api.goal_state")
    agent_reload = body.find("pywebview.api.goal_ai_state")
    clear_pending = body.find("goalFocusPending=null")
    assert -1 not in (review_pending, review_call, reload_call, agent_reload, clear_pending)
    assert review_pending < review_call < reload_call < agent_reload < clear_pending


def test_investigation_synthesis_is_explicit_and_reviewable():
    script = _script()
    render = _function_body(script, "curSynthesisHtml")
    bind = _function_body(script, "bindCurCard")

    assert "cur.synthesis_due" in render
    assert "new experiment outcome" in render
    assert "Previous approved interpretation" in render
    assert "Approve edited interpretation" in render
    assert "Review with new evidence" in render
    assert "curiosity_synthesize" in bind
    assert "curiosity_synthesis_decide" in bind
    assert "curiosity_person_reconcile" in bind
    assert "curiosity_person_proposal" in bind
    assert "prior wording remains in history" in bind


def test_suggested_investigations_are_bounded_and_never_autostart():
    script = _script()
    render = _function_body(script, "curCandidatePanelHtml")
    bind = _function_body(script, "bindCandidatePanel")

    assert "2 shown" in render
    assert "Nothing starts automatically" in render
    assert "Never suggest this topic" in render
    assert "What this could change" in render
    assert "curiosity_candidate_suggest" in bind
    assert "curiosity_candidate_action" in bind
    assert "candidate-start" in bind
    assert "sensitive topic" in bind
    assert "Starting Investigation" in bind
    assert "instead of creating a duplicate" in bind


def test_related_investigations_can_be_compared_and_merged_with_loading_feedback():
    html = _html()
    script = _script()
    render = _function_body(script, "curRelatedPanelHtml")
    bind = _function_body(script, "bindRelatedInvestigationPanel")

    assert 'id="cur-related"' in html
    assert "Comparing active Investigations for overlap" in render
    assert "Questions, answers, interpretations, outcomes, and Growth links" in render
    assert "curiosity_related_investigations" in bind
    assert "curiosity_merge" in bind
    assert "Combining while preserving history" in bind
    assert "기록을 보존하며 통합 중" in bind


def test_investigation_exploration_threads_separate_directions_and_roll_up():
    html = _html()
    script = _script()
    threads = _function_body(script, "curThreadsHtml")
    card = _function_body(script, "curCardHtml")
    bind = _function_body(script, "bindCurCard")
    candidates = _function_body(script, "curCandidatePanelHtml")

    assert "Exploration Threads" in threads and "탐색 갈래" in threads
    assert "Learning from each thread rolls up into the parent interpretation" in threads
    assert "curThreadsHtml(cur)" in card
    assert "curiosity_thread_create" in script
    assert "curiosity_thread_generate" in script
    assert "curiosity_thread_status" in script
    assert "curiosity_item_thread" in script
    assert "Question belongs in" in script and "이 질문의 위치" in script
    assert "candidate-direction" in candidates
    assert "Create a thread inside" in candidates
    assert "Start as a separate Investigation" in candidates


def test_tree_gardening_explains_new_evidence_and_requires_approval():
    script = _script()
    relevance = _function_body(script, "goalRelevanceHtml")
    bind = _function_body(script, "bindGoalRelevanceControls")
    history = _function_body(script, "goalArchivedHistoryHtml")

    assert "Newer evidence behind this prompt" in relevance
    assert "Gardening proposals" in relevance
    assert "Prior relevance reviews" in relevance
    assert "goal_relevance_review" in bind
    assert "goal_gardening_proposal" in bind
    assert "Tree change approved; history was preserved." in bind
    assert "Archived history" in history
    assert "without cluttering the current map" in history


def test_leaf_outcomes_capture_learning_and_feed_the_next_experiment():
    script = _script()
    render = _function_body(script, "goalOutcomeHtml")
    bind = _function_body(script, "bindGoalOutcomeControls")
    focus = _function_body(script, "bindGoalFocusPanel")

    for phrase in ("What happened?", "What obstacle did you expect?",
                   "What surprised you?", "How helpful was this?",
                   "What changed in your understanding?",
                   "What should the next experiment adjust?"):
        assert phrase in render
    assert "Completed" in render and "Avoided" in render and "Abandoned intentionally" in render
    assert "Next experiment should reflect this" in render
    assert "draft is autosaved before submission" in render
    assert "goal_experiment_outcome" in bind
    assert "restoreGoalOutcomeDraft" in bind
    assert "saveGoalOutcomeDraft" in bind
    assert "clearGoalOutcomeDraft" in bind
    assert "lower-confidence interpretation is ready for review" in bind
    assert "Record what happened so Faerie can learn" in focus


def test_leaf_outcome_draft_survives_rerenders_until_a_successful_save():
    script = _script()
    save = _function_body(script, "saveGoalOutcomeDraft")
    restore = _function_body(script, "restoreGoalOutcomeDraft")
    bind = _function_body(script, "bindGoalOutcomeControls")

    assert "localStorage.setItem" in save
    assert "localStorage.getItem" in restore
    assert "form.open=true" in restore
    assert "field.addEventListener('input'" in bind
    assert "field.addEventListener('change'" in bind
    assert bind.find("if(!r||r.ok===false)") < bind.find("clearGoalOutcomeDraft")


def test_new_growth_nodes_use_definition_first_progressive_disclosure():
    script = _script()
    gate = _function_body(script, "goalNodeNeedsDefinition")
    starter = _function_body(script, "goalDefinitionStarterHtml")
    render = _function_body(script, "renderGoalFocusPanel")

    assert "node.type==='umbrella'" in gate
    assert "node.description" in gate and "node.notes" in gate
    assert "node.children" in gate and "node.evidence" in gate
    assert "node.curiosities" in gate and "node.outcomes" in gate
    assert "origin.summary" in gate and "origin.source_label" in gate
    assert "What this Leaf asks you to do" in _function_body(script, "goalDefinitionLabel")
    assert "node.type==='task'" in starter and "goalLeafStepsHtml(node)" in starter
    assert "goal-focus-draft-steps" in starter
    assert "goalNodeNeedsDefinition(node)" in render
    assert render.find("goalNodeNeedsDefinition(node)") < render.find("goalRecapHtml(")
    assert "return;" in render[render.find("goalNodeNeedsDefinition(node)"):render.find("goalRecapHtml(")]


def test_goalai_step_drafts_remain_available_for_non_leaf_definition_work():
    script = _script()
    leaf_steps = _function_body(script, "goalLeafStepsHtml")
    starter = _function_body(script, "goalDefinitionStarterHtml")
    draft = _function_body(script, "draftLeafSteps")
    persisted = _function_body(script, "persistGoalStepDraft")
    draft_html = _function_body(script, "goalStepDraftHtml")
    bind = _function_body(script, "bindGoalStepDraft")

    assert "goal-open-leaf-agent" in leaf_steps
    assert "goal-focus-draft-steps" not in leaf_steps
    assert 'class="accent" id="goal-focus-draft-steps"' in starter
    assert "ffGoalStepDrafts" in script
    assert "localStorage.setItem('ffGoalStepDrafts'" in persisted
    assert "goal_leaf_step_draft(node.id)" in draft
    assert "persistGoalStepDraft(node.id,text,draft)" in draft
    assert "goal_ai_chat(node.id" in draft
    assert "renderGoalFocusPanel()" in draft
    assert "goalStepDraftHtml(node)" in starter
    assert "even if you change tabs or reopen the app" in draft_html
    assert "탭을 이동하거나 앱을 다시 열어도 사라지지 않아요" in draft_html
    assert "LEAF BOUNDARY" in draft_html
    assert "Output owned by this Leaf" in draft_html
    assert "Responsibility overlap with nearby Leaves" in draft_html
    assert "Create merge proposal" in draft_html
    assert "goal_leaf_merge_proposal" in bind
    assert "Create narrowing proposal" in draft_html
    assert "goal_leaf_rewrite_proposal" in bind
    assert "Nothing changes until you approve it in Growth" in bind
    assert "root.querySelector('.goal-steps-save')" in bind
    assert "persistGoalStepDraft(node.id,'')" in bind
    assert "$('goal-steps-save').onclick" not in draft


def test_growth_map_skin_switches_between_tree_and_solar_system():
    html = _html()
    script = _script()
    setting = _function_body(script, "setGrowthMapSkin")
    constellation = _function_body(script, "renderGoalConstellation")
    layout = _function_body(script, "buildGoalConstellation")

    assert 'id="settings-growth-skin"' in html
    assert '<option value="tree">Living tree</option>' in html
    assert '<option value="solar">Solar system</option>' in html
    assert "ffGrowthMapSkin" in script
    assert "localStorage.setItem('ffGrowthMapSkin'" in setting
    assert "classList.toggle('skin-solar'" in setting
    assert "box.classList.toggle('skin-solar'" in constellation
    assert "solar-orbit" in constellation
    assert "solarSun" in constellation and "solarPlanet" in constellation
    assert "planet-shine" in constellation
    assert "rings" in layout
    assert ".growth-map-tree.skin-solar" in html
    assert "'Growth map style':'성장 지도 스타일'" in script
    assert "'Solar system':'태양계'" in script


def test_leaf_panel_has_one_agent_entry_and_hides_step_checklist_controls():
    html = _html()
    script = _script()
    steps = _function_body(script, "goalLeafStepsHtml")
    entry = _function_body(script, "bindGoalLeafAgentEntry")
    render_coach = _function_body(script, "renderLeafCoach")

    assert 'id="leaf-step-coach"' in html
    assert "Open Leaf Agent" in steps and "Leaf 에이전트 열기" in steps
    assert "goal-step-help-all" not in steps
    assert "data-step-help" not in steps
    assert "goal-step-check" not in steps
    assert "How to do this" not in steps
    assert "openLeafCoach(node)" in entry
    assert "localStorage" not in entry
    assert "cannot see other Branches, global memory, the main chat, or screen activity" in render_coach
    assert "다른 Branch, 전체 기억, 메인 채팅 또는 화면 활동은 볼 수 없어요" in render_coach


def test_primary_navigation_uses_compact_icon_rows_instead_of_pills():
    html = _html()

    assert "nav .tab::before,.group-label::before" in html
    assert 'nav .tab[data-view="self"]::before' in html
    assert 'nav .tab[data-view="goals"]::before' in html
    assert 'nav .tab[data-view="curiosity"]::before' in html
    assert "border-radius:7px" in html
    assert "background:rgba(23,28,25,.94)" in html
    assert "box-shadow:inset 2px 0 0 var(--green)" in html


def test_all_buttons_and_navigation_controls_receive_quick_hover_help():
    html = _html()
    script = _script()
    helper = _function_body(script, "buttonHelpText")
    show = _function_body(script, "showButtonHelp")

    assert 'id="button-help-tooltip" role="tooltip"' in html
    assert "#button-help-tooltip.visible" in html
    assert "goal-open-leaf-agent" in helper
    assert "goal-add-something" in helper
    assert "goal-root-starters-open" in helper
    assert "restructure" in helper
    assert "without deleting attached history" in helper
    assert "첨부된 기록을 삭제하지 않고" in helper
    assert "nav .tab" in helper and ".group-label" in helper
    assert "aria-description" in show
    assert "button,nav .tab,.group-label" in script
    assert "},140)" in show


def test_leaf_workspace_renders_optional_suggestions_retry_and_clear():
    html = _html()
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")
    error = _function_body(script, "leafCoachSetError")

    assert "leaf-workspace-suggestion" in message
    assert "message.content||payload.content||payload.text" in message
    assert "payload.next_action" not in message and "payload.question" not in message
    assert "data-coach-shortcut" not in html
    assert "leaf-coach-retry" in error
    assert "goal_leaf_workspace_clear" in script


def test_leaf_coach_open_failure_clears_stale_loading_message():
    script = _script()
    open_coach = _function_body(script, "openLeafCoach")

    assert "$('leaf-coach-messages').innerHTML='';" in open_coach
    assert "leafCoachSetError(String(error.message||error),run);" in open_coach


def test_leaf_workspace_offers_stable_responses_and_reviewable_proposals():
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")
    decision = _function_body(script, "decideLeafWorkspaceProposal")

    assert "Suggestions" in message and "제안" in message
    assert "data-message-id" in message and "data-suggestion-id" in message
    assert "leafWorkspaceProposalHtml" in message
    assert "goal_leaf_workspace_decide" in decision
    assert "editedPayload" in decision


def test_text_is_selectable_and_right_click_has_copy_paste_menu():
    html = _html()
    script = _script()

    assert "user-select:text" in html
    assert ".text-context-menu" in html
    assert "installTextContextMenu" in script
    assert "clipboard_write" in script and "clipboard_read" in script
    assert "contextmenu" in script
    assert "text_select=True" in (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "confirmed resolutions will remain" in script


def test_leaf_coach_close_stays_fixed_and_uses_an_accessible_x():
    html = _html()

    assert 'class="cur-head leaf-coach-head"' in html
    assert 'class="leaf-coach-scroll"' in html
    assert 'aria-label="Close Leaf Agent"' in html
    assert '>×</button>' in html
    assert ".leaf-coach-drawer.open { display:flex; flex-direction:column; }" in html
    assert ".leaf-coach-scroll { flex:1 1 auto; min-height:0; overflow-y:auto;" in html


def test_leaf_workspace_completion_is_an_explicit_proposal_decision():
    script = _script()
    labels = _function_body(script, "leafWorkspaceProposalTypeLabel")
    proposal = _function_body(script, "leafWorkspaceProposalHtml")
    decision = _function_body(script, "decideLeafWorkspaceProposal")

    assert "complete_item" in labels and "complete_leaf" in labels
    assert "reshape" in labels and "reopen" in labels
    assert 'data-proposal-decision="approve"' in proposal
    assert "Keep discussing" in proposal and "계속 논의" in proposal
    assert "goal_leaf_workspace_decide" in decision


def test_leaf_workspace_does_not_use_local_storage_as_completion_authority():
    script = _script()
    open_workspace = _function_body(script, "openLeafCoach")
    send = _function_body(script, "sendLeafCoachMessage")

    assert "localStorage" not in open_workspace
    assert "localStorage" not in send
    assert "syncLeafCoachStepCompletion" not in open_workspace
    assert "syncLeafCoachStepCompletion" not in send


def test_leaf_workspace_lifecycle_cards_and_phase_aware_composer():
    html = _html()
    script = _script()
    agreement = _function_body(script, "leafWorkspaceAgreementHtml")
    plan = _function_body(script, "leafWorkspacePlanHtml")
    placeholder = _function_body(script, "leafWorkspacePlaceholder")

    assert "Current agreement" in agreement and "현재 합의" in agreement
    assert "Outcome" in agreement and "Approach" in agreement
    assert "Definition of done" in agreement
    assert "Confirmed constraints" in agreement and "확인된 제약" in agreement
    assert "Confirmed result" in agreement and "Lesson" in agreement
    assert "Working understanding · not approved yet" in agreement
    assert "작업 중인 이해 · 아직 승인되지 않음" in agreement
    assert "<details" in agreement and "leaf-workspace-agreement-preview" in agreement
    assert "stepsAt" in agreement
    assert "Approved plan" in plan and "승인된 계획" in plan
    assert "item.status" in plan and "item.resolution" in plan
    for phase in ("approve", "approval", "work", "working", "doing", "reflect", "reflecting", "complete"):
        assert phase in placeholder
    phase_labels = _function_body(script, "leafWorkspacePhaseLabel")
    assert "shaping" in phase_labels and "reflecting" in phase_labels
    kind_labels = _function_body(script, "leafWorkspaceKindLabel")
    assert "unspecified" in kind_labels and "Open mode" in kind_labels
    rendered = _function_body(script, "renderLeafCoach")
    assert "view.completed" in rendered and "Completed" in rendered and "완료됨" in rendered
    assert "leaf-workspace-composer" in html
    assert ".leaf-workspace-composer { flex:0 0 auto;" in html


def test_leaf_workspace_natural_messages_do_not_require_suggestions():
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")

    assert "message.content||payload.content||payload.text" in message
    assert "(content?'<div>'+conversationHtml(content)" in message
    assert "!suggestions.length" in message
    assert "leafWorkspaceMessageSuggestions" in message
    assert "suggestionsHtml" in message


def test_leaf_workspace_suggestion_selection_sends_stable_structured_event():
    script = _script()
    bind = _function_body(script, "leafWorkspaceBindActions")

    assert "kind:'suggestion_selected'" in bind
    assert "suggestion_id:suggestion.id" in bind
    assert "label:suggestion.label" in bind
    assert "message_id:message.id" in bind
    assert "sendLeafCoachMessage(suggestion.label" in bind
    assert "button.classList.add('selected')" in bind
    assert "Selected “" in bind and "thinking" in bind


def test_leaf_workspace_multiple_suggestions_use_checkboxes_and_one_submit():
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")
    bind = _function_body(script, "leafWorkspaceBindActions")
    mode = _function_body(script, "leafWorkspaceMessageSelectionMode")

    assert "selection_mode" in mode and "multiple" in mode
    assert "any of (?:these|the following)" in mode
    assert 'type="checkbox" data-multi-suggestion' in message
    assert "Select all that apply" in message and "해당하는 항목을 모두 선택하세요" in message
    assert "Submit selected" in message and "선택 항목 제출" in message
    assert "data-submit-suggestions disabled" in message
    assert "checks.some(item=>item.checked)" in bind
    assert "kind:'select_suggestions'" in bind
    assert "suggestion_ids:selected.map(item=>item.id)" in bind


def test_conversations_bundle_atkinson_hyperlegible_regular_and_bold():
    html = _html()

    font_dir = ROOT / "livingpc" / "ui" / "assets" / "fonts"
    assert (font_dir / "AtkinsonHyperlegible-Regular.ttf").is_file()
    assert (font_dir / "AtkinsonHyperlegible-Bold.ttf").is_file()
    assert (font_dir / "OFL.txt").is_file()
    assert html.count("@font-face") >= 2
    assert "AtkinsonHyperlegible-Regular.ttf" in html
    assert "AtkinsonHyperlegible-Bold.ttf" in html
    assert "function conversationHtml" in html and "<strong>$1</strong>" in html


def test_atkinson_hyperlegible_is_the_default_across_every_ui_surface():
    ui_dir = ROOT / "livingpc" / "ui"
    surfaces = {
        name: (ui_dir / name).read_text(encoding="utf-8")
        for name in ("memory.html", "agent_window.html", "assistant.html", "capture.html")
    }

    for name, html in surfaces.items():
        assert html.count("@font-face") >= 2, name
        assert "AtkinsonHyperlegible-Regular.ttf" in html, name
        assert "AtkinsonHyperlegible-Bold.ttf" in html, name
        assert "--app-font:'Atkinson Hyperlegible'" in html, name
        assert "Consolas" not in html and "Cascadia Mono" not in html, name
        assert "system-ui" not in html, name

    main = surfaces["memory.html"]
    assert "button,input,textarea,select,option,summary,pre,code,kbd,samp,svg text" in main
    assert "font-family:var(--app-font)" in main
    assert "font:12px/1.55 var(--app-font)" in main
    assert "font:12px/1.6 var(--app-font)" in main


def test_leaf_workspace_scope_nonce_ignores_late_leaf_results():
    script = _script()
    opened = _function_body(script, "openLeafCoach")
    sent = _function_body(script, "sendLeafCoachMessage")
    closed = _function_body(script, "closeLeafCoach")

    assert "nonce=++leafWorkspaceRequestNonce" in opened
    assert "leafCoachView=null" in opened
    assert "leafWorkspaceSetEnabled(false)" in opened
    assert "nonce!==leafWorkspaceRequestNonce" in opened
    assert "String(leafCoachLeafId)!==String(node.id)" in opened
    assert "nonce!==leafWorkspaceRequestNonce" in sent
    assert "String(leafCoachLeafId)!==String(leafId)" in sent
    assert "leafWorkspaceRequestNonce++" in closed
    assert "leafCoachView=null" in closed


def test_leaf_workspace_clear_copy_preserves_approved_state_in_both_languages():
    script = _script()
    clear = _function_body(script, "clearLeafWorkspaceConversation")

    assert "goal_leaf_workspace_clear" in clear
    assert "Clear only this Leaf’s conversation?" in clear
    assert "approved agreement, plan, progress, and confirmed resolutions will remain" in clear
    assert "이 Leaf의 대화만 지울까요?" in clear
    assert "승인된 합의, 계획, 진행 상태와 확인된 해결 기록은 유지돼요" in clear
    assert "openLeafCoach" not in clear


def test_leaf_workspace_legacy_history_is_collapsed_and_read_only():
    html = _html()
    script = _script()
    legacy = _function_body(script, "leafWorkspaceLegacyHtml")

    assert "<details" in legacy and "<summary>" in legacy
    assert "Earlier Leaf coaching conversation" in legacy
    assert "이전 Leaf 코칭 대화" in legacy
    assert "button" not in legacy and "textarea" not in legacy
    assert 'id="leaf-workspace-legacy"' in html


def test_leaf_workspace_proposal_review_supports_edit_approve_and_discussion():
    script = _script()
    editor = _function_body(script, "leafWorkspaceProposalEditorHtml")
    proposal = _function_body(script, "leafWorkspaceProposalHtml")
    edited = _function_body(script, "leafWorkspaceEditedPayload")

    for proposal_type in ("agreement", "plan", "revise_plan", "complete_item", "complete_leaf"):
        assert proposal_type in editor
    assert "data-proposal-edit" in proposal
    assert 'data-proposal-decision="keep_discussing"' in proposal
    assert 'data-proposal-decision="approve"' in proposal
    assert "Edit" in proposal and "편집" in proposal
    assert "Apply plan" in proposal and "계획 적용" in proposal
    assert "data-proposal-field" in edited
    assert "Suggested confirmed result" in editor
    assert "Suggested lesson" in editor
    assert "resultSuggestion" in editor and "lessonSuggestion" in editor


def test_leaf_workspace_completion_edits_survive_chat_rerenders_until_decided():
    script = _script()
    save = _function_body(script, "saveLeafWorkspaceProposalDraft")
    restore = _function_body(script, "restoreLeafWorkspaceProposalDraft")
    bind = _function_body(script, "leafWorkspaceBindActions")
    decide = _function_body(script, "decideLeafWorkspaceProposal")

    assert "localStorage.setItem" in save
    assert "localStorage.getItem" in restore
    assert "restoreLeafWorkspaceProposalDraft" in bind
    assert "saveLeafWorkspaceProposalDraft" in bind
    assert "clearLeafWorkspaceProposalDraft" in decide


def test_leaf_workspace_loading_is_visible_and_retry_preserves_the_draft():
    script = _script()
    loading = _function_body(script, "leafWorkspaceSetLoading")
    send = _function_body(script, "sendLeafCoachMessage")

    assert "thinkingDotsHtml" in loading and "Crafting a response" in loading
    assert "Faerie is crafting a response" in send and "페어리가 답변을 만드는 중" in send
    assert "optimistic.dataset.optimistic='true'" in send
    assert "if(input){input.value=''" in send
    assert "input.style.height='auto';autoGrow(input)" in send
    assert "input.value=text" in send
    assert "leafCoachSetError(String(error.message||error),run)" in send


def test_leaf_workspace_is_resizable_and_enter_sends_without_losing_shift_enter():
    html = _html()
    script = _script()
    resize = _function_body(script, "initLeafCoachResize")

    assert 'id="leaf-coach-resize"' in html
    assert "cursor:ew-resize" in html
    assert "pointerdown" in resize and "pointermove" in resize and "pointerup" in resize
    assert "faerie_leaf_agent_width" in resize
    assert "event.key==='Enter'&&!event.shiftKey&&!event.isComposing" in script


def test_chat_surfaces_use_a_shared_animated_thinking_state():
    html = _html()
    script = _script()

    assert "function thinkingDotsHtml" in script
    assert "@keyframes thinking-dot" in html
    assert "inquiry-thinking" in script
    assert "goal-agent-thinking" in script
    assert "plannerThinking" in script and "thinkingDotsHtml(text)" in script
    assert "Faerie is crafting a response" in script


def test_general_faerie_button_uses_the_real_growth_type_and_visible_context():
    script = _script()
    label = _function_body(script, "goalAskFaerieLabel")
    focus = _function_body(script, "renderGoalFocusPanel")
    bind = _function_body(script, "bindGoalFocusPanel")

    assert "goalTypeLabel" in label
    assert "Ask Faerie about this " in label
    assert "이 '+type+'에 대해 페어리에게 묻기" in label
    assert "goalAskFaerieLabel(node)" in focus
    assert "node.description" in bind and "goalLeafSteps(node)" in bind


def test_pixel_faerie_mascot_is_code_native_responsive_and_accessible():
    html = _html()

    assert 'id="faerie-mascot"' in html
    assert 'aria-label="Open Faerie chat"' in html
    assert '<svg class="mascot-classic" viewBox="0 0 80 80"' in html
    assert "mascot-wing wing-left" in html and "mascot-wing wing-right" in html
    assert "mascot-eye" in html and "mascot-spark" in html
    assert "@keyframes mascot-float" in html
    assert "@keyframes mascot-thinking" in html
    assert "@keyframes mascot-reply" in html
    assert "@keyframes mascot-error" in html
    assert "@media(max-width:1080px){ #faerie-mascot" in html
    assert "@media(prefers-reduced-motion:reduce)" in html
    assert "#faerie-mascot,#faerie-mascot * { animation:none !important; }" in html


def test_mascot_giggles_and_sprays_sparks_only_while_hovered():
    html = _html()

    assert "mascot-giggle-face" in html
    assert html.count('class="mascot-giggle-drop"') == 4
    assert "@keyframes mascot-giggle-eight" in html
    assert "@keyframes mascot-giggle-spray" in html
    assert '#faerie-mascot[data-state="idle"]:not(.dragging):hover' in html
    assert ':hover .mascot-giggle-drop' in html
    assert "#faerie-mascot.dragging .mascot-giggle-drop" in html
    assert "#faerie-mascot.dragging .mascot-giggle-face" in html
    assert "prefers-reduced-motion:reduce" in html


def test_mascot_has_soft_feminine_pixel_details():
    html = _html()

    assert "mascot-hair" in html
    assert "mascot-bow" in html
    assert "mascot-lash" in html
    assert "mascot-skirt" in html


def test_mascot_visibility_setting_persists_and_defaults_on():
    html = _html()
    script = _script()
    refresh = _function_body(script, "refreshFaerieMascotSetting")
    setter = _function_body(script, "setFaerieMascotEnabled")

    assert 'id="settings-mascot-enabled" checked' in html
    assert "ffFaerieMascotEnabled" in script
    assert "!=='0'" in script
    assert "mascot-disabled" in refresh
    assert "localStorage.setItem('ffFaerieMascotEnabled'" in setter
    assert "'Show Faerie mascot':'페어리 마스코트 표시'" in script
    assert "'Open Faerie chat':'페어리 채팅 열기'" in script


def test_dark_fae_mascot_skin_is_selectable_persistent_and_animated():
    html = _html()
    script = _script()
    refresh = _function_body(script, "refreshFaerieMascotSetting")
    setter = _function_body(script, "setFaerieMascotSkin")
    asset = ROOT / "livingpc" / "ui" / "assets" / "dark-fae-mascot.png"

    assert asset.exists() and asset.stat().st_size > 50_000
    assert asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert 'id="settings-mascot-skin"' in html
    assert '<option value="classic">Pixel Faerie</option>' in html
    assert '<option value="dark">Dark flame faerie</option>' in html
    assert 'src="assets/dark-fae-mascot.png"' in html
    assert "dark-fae-wing-echo" in html and "dark-fae-flame" in html
    assert "@keyframes dark-fae-wings" in html
    assert "@keyframes dark-fae-flame" in html
    assert "@keyframes dark-fae-mote" in html
    assert "ffFaerieMascotSkin" in script
    assert "mascot.dataset.skin=faerieMascotSkin" in refresh
    assert "localStorage.setItem('ffFaerieMascotSkin'" in setter
    assert "persistDurableUiPreference('mascot_skin'" in setter
    assert "ui_preferences_get" in script
    assert "hydrateDurableUiPreferences().then(()=>info)" in script
    assert "'Faerie style':'페어리 스타일'" in script
    assert "'Dark flame faerie':'어둠의 불꽃 페어리'" in script


def test_pixel_cupid_cat_skin_is_transparent_selectable_and_animated():
    html = _html()
    script = _script()
    asset = ROOT / "livingpc" / "ui" / "assets" / "cupid-cat-mascot.png"

    assert asset.exists() and asset.stat().st_size > 50_000
    assert asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert '<option value="cat">Cupid flower cat</option>' in html
    assert 'src="assets/cupid-cat-mascot.png"' in html
    assert "cupid-cat-aura" in html and "cupid-cat-heart" in html
    assert "@keyframes cupid-cat-breathe" in html
    assert "@keyframes cupid-cat-heart" in html
    assert "['classic','dark','cat','knight','meditate'].includes" in script
    assert "'Cupid flower cat':'큐피드 꽃 고양이'" in script


def test_knight_and_meditating_pixel_cat_skins_are_transparent_and_selectable():
    html = _html()
    script = _script()
    assets = [
        ROOT / "livingpc" / "ui" / "assets" / "knight-cat-mascot.png",
        ROOT / "livingpc" / "ui" / "assets" / "meditating-cat-mascot.png",
    ]

    for asset in assets:
        raw = asset.read_bytes()
        assert asset.exists() and asset.stat().st_size > 50_000
        assert raw.startswith(b"\x89PNG\r\n\x1a\n") and raw[25] == 6  # RGBA PNG
    assert '<option value="knight">Knight star cat</option>' in html
    assert '<option value="meditate">Meditating lotus cat</option>' in html
    assert 'src="assets/knight-cat-mascot.png"' in html
    assert 'src="assets/meditating-cat-mascot.png"' in html
    assert "knight-cat-skin" in html and "meditating-cat-skin" in html
    assert "@keyframes pixel-cat-breathe" in html
    assert "@keyframes pixel-cat-particle" in html
    assert "'Knight star cat':'별의 기사 고양이'" in script
    assert "'Meditating lotus cat':'명상하는 연꽃 고양이'" in script


def test_every_mascot_skin_mirrors_toward_cursor_or_screen_center():
    html = _html()
    script = _script()
    facing = _function_body(script, "updateFaerieMascotFacing")
    drag = _function_body(script, "bindFaerieMascotDrag")

    assert 'class="mascot-facing"' in html
    assert "transform:scaleX(var(--mascot-facing,1))" in html
    assert "target<(rect.left+rect.width/2)" in facing
    assert "--mascot-facing" in facing and "dataset.facing" in facing
    assert "window.innerWidth/2" in drag
    assert "window.addEventListener('pointermove'" in drag
    assert "updateFaerieMascotFacing(event.clientX)" in drag


def test_mascot_opens_main_chat_and_tracks_chat_lifecycle():
    script = _script()
    send = _function_body(script, "sendCommandMessage")
    approve = _function_body(script, "approvePendingProposal")

    assert "$('faerie-mascot').onclick" in script
    assert "switchView('self')" in script
    assert "$('cc-input')" in script and ".focus()" in script
    for body in (send, approve):
        assert "setFaerieMascotState('thinking')" in body
        assert "setFaerieMascotState('reply',1200)" in body
        assert "setFaerieMascotState('error',1100)" in body


def test_mascot_is_draggable_clamped_and_persists_its_position():
    html = _html()
    script = _script()
    drag = _function_body(script, "bindFaerieMascotDrag")
    apply_position = _function_body(script, "applyFaerieMascotPosition")

    assert "cursor:grab" in html and "touch-action:none" in html
    assert "pointerdown" in drag and "pointermove" in drag and "pointerup" in drag
    assert "Math.hypot(dx,dy)<5" in drag
    assert "ffFaerieMascotPosition" in script
    assert "window.innerWidth" in drag and "window.innerHeight" in drag
    assert "faerieMascotSuppressClick" in script
    assert "style.right='auto'" in apply_position


def test_mascot_hides_for_overlays_and_drawers():
    html = _html()

    assert "body:has(.planner-drawer.open) #faerie-mascot" in html
    assert "body:has(.inquiry-drawer.open) #faerie-mascot" in html
    assert "body:has(.onboard-overlay.open) #faerie-mascot" in html
    assert "body:has(.growth-map-overlay.open) #faerie-mascot" in html
    assert "pointer-events:none" in html


def test_outcome_form_fields_stack_at_full_width():
    html = _html()
    assert ".outcome-form > label { display:grid; grid-template-columns:1fr" in html
    assert ".outcome-form textarea,.outcome-form select { display:block; width:100%; min-width:0;" in html
    assert ".outcome-form textarea { min-height:68px; resize:vertical;" in html


def test_suggestions_review_overlap_before_creating_or_adapting_a_goal_node():
    script = _script()
    suggestion = _function_body(script, "curSuggestionHtml")
    review = _function_body(script, "reviewSuggestionImplementation")
    adapt = _function_body(script, "renderSuggestionAdaptForm")

    assert "Review implementation…" in suggestion
    assert "cur-overlap-review" in suggestion
    assert "curiosity_suggestion_overlap" in review
    assert "cur-adapt-leaf" in script
    assert "Create a separate plan" in script
    assert "Recognized from the earlier implementation" in script
    assert "Data stays attached" in script
    assert "Existing steps, completion, coaching history, evidence, outcomes, and children remain attached" in script
    assert "No clear existing goal match was found" in script
    assert "Choose placement" in script
    assert "curiosity_suggestion_propose_update" in adapt
    assert "This does not create a new node" in adapt
    assert "same node" in adapt
    assert "wording-and-concept comparison, not merge progress" in script
    assert "wording-overlap estimate" in script
    assert "Apply update now" in adapt
    assert "goal_ai_proposal(result.proposal_id,'approve'" in adapt
    assert "loadGoals()" in adapt and "loadCuriosity()" in adapt
    assert "문구·개념 중복 추정" in script
    assert "지금 업데이트 적용" in adapt
    assert "트리에 이미 있는 작업과 겹칠 수 있어요" in script


def test_new_suggestion_requires_semantic_placement_review_before_planning():
    script = _script()
    placement = _function_body(script, "renderSuggestionPlacement")
    request = _function_body(script, "reviewSuggestionPlacement")
    planner = _function_body(script, "openGoalPlanner")

    assert "goal_plan_placement" in request
    assert "Finding the best place in your Growth tree" in request
    assert "Where should this plan live?" in placement
    assert "not merely similar wording" in placement
    assert "A temporary project cannot become a Root" in placement
    assert "Proposed path" in placement
    assert "One placement question" in placement
    assert "new_root" in placement and "root_eligible:true" in placement
    assert "Continue with this placement" in placement
    assert "이 계획은 어디에 속하나요?" in placement
    assert "만들어질 경로" in placement
    assert "배치 분석 중" in request
    assert "goal_plan_start(itemId,targetParentId,placement||{})" in planner


def test_growth_restructure_flow_previews_preserved_data_before_approval():
    script = _script()
    panel = _function_body(script, "renderGoalRestructurePanel")
    manual = _function_body(script, "renderGoalRestructureManual")
    proposal = _function_body(script, "goalProposalSummaryHtml")
    detail = _function_body(script, "renderGoalDetail")
    focus = _function_body(script, "renderGoalFocusPanel")
    bind_focus = _function_body(script, "bindGoalFocusPanel")

    assert "goal_tree_restructure_recommend" in panel
    assert "Faerie’s whole-path restructure" in panel
    assert "Create whole-tree proposal" in panel
    assert "goal_tree_restructure_propose" in panel
    assert "Approve all structure changes" in panel
    assert "Adjust only this node" in panel
    assert "페어리의 전체 경로 구조 제안" in panel
    assert "모든 구조 변경 승인" in panel
    assert "goal_restructure_preview" in manual
    assert "goal_restructure_propose" in manual
    assert "restructureKinds=['root','area','project','stage','leaf']" in manual
    assert "semanticRole:['area','project','stage'].includes(kind)?kind:null" in manual
    assert "structure.semanticRole" in manual
    assert "candidate.semantic_role==='area'" in manual
    assert "candidate.semantic_role==='project'" in manual
    assert "Nothing is deleted or recreated" in manual
    assert "The same node ID will be preserved" in manual
    assert "Create proposal for approval" in manual
    assert "The tree has not changed" in manual
    assert "구조 안전하게 변경" in manual
    assert "같은 노드 ID가 유지됩니다" in manual
    assert "restructure_node" in proposal and "restructure_tree" in proposal
    assert "Restructure without losing data" in proposal
    assert "Whole-path restructure" in proposal
    assert "Proposed Growth addition" in proposal
    assert "It will not enter the map until you approve it" in proposal
    assert "Approve restructure" in script
    assert "goal-restructure-open" in detail
    assert "goal-focus-restructure" in focus
    assert "goal-focus-restructure-panel" in focus
    assert "goal-focus-ask-command" in focus and "Restructure" in focus
    assert "renderGoalRestructurePanel(node,$('goal-focus-restructure-panel'))" in bind_focus
    assert "#goal-focus-panel .agent-proposal[data-pid]" in bind_focus


def test_growth_nodes_have_reversible_archive_controls_in_both_views_and_languages():
    script = _script()
    focus = _function_body(script, "renderGoalFocusPanel")
    detail = _function_body(script, "renderGoalDetail")
    lifecycle = _function_body(script, "goalLifecycleButtonHtml")
    binding = _function_body(script, "bindGoalArchiveControls")

    assert "goalLifecycleActionHtml(node)" in focus
    assert "goalLifecycleButtonHtml(node)" in detail
    assert "goal-node-archive" in lifecycle and "goal-node-restore" in lifecycle
    assert "Archive this node" in lifecycle and "Restore this node" in lifecycle
    assert "이 항목 보관" in lifecycle and "이 항목 복원" in lifecycle
    assert "goal_archive_prepare(node.id)" in binding
    assert "goal_archive(node.id,harvest.id)" in binding and "goal_restore(node.id)" in binding
    assert "Preparing knowledge handoff" in binding
    assert "only the reviewed summary flows upward" in binding
    assert "completion states" in binding and "완료 상태" in binding
    assert "goalFindParent(goalState.tree,node.id)" in binding
    assert "statusChoices=node.status==='archived'?['archived']:['active','paused','completed']" in detail


def test_nested_branch_roles_render_as_area_project_and_stage_in_both_languages():
    html = _html()
    script = _script()
    labels = _function_body(script, "goalTypeLabel")
    semantic = _function_body(script, "goalSemanticRoleLabel")
    constellation = _function_body(script, "renderGoalConstellation")

    assert "node.semantic_role" in labels
    assert "area:'Area'" in semantic and "project:'Project'" in semantic and "stage:'Stage'" in semantic
    assert "area:'영역'" in semantic and "project:'프로젝트'" in semantic and "stage:'단계'" in semantic
    assert "goalTypeLabel(node.type,node)" in constellation
    assert 'class="legend-area">◇ Area' in html
    assert 'class="legend-project">◇ Project' in html
    assert 'class="legend-stage">◇ Stage' in html
    assert "'◇ Area':'◇ 영역'" in script
    assert "'◇ Project':'◇ 프로젝트'" in script
    assert "'◇ Stage':'◇ 단계'" in script
    assert "semantic-area" in html and "semantic-project" in html and "semantic-stage" in html
    assert "roleRadius={area:9.5,project:8,stage:6.5}" in constellation


def test_growth_creation_uses_optional_roots_and_plain_language_ai_intake():
    script = _script()
    focus = _function_body(script, "renderGoalFocusPanel")
    detail = _function_body(script, "renderGoalDetail")
    controls = _function_body(script, "bindGoalCreationControls")
    starters = _function_body(script, "renderGoalRootStarters")
    intake = _function_body(script, "renderGoalIntake")

    assert "Set up starter Roots" in focus and "New Root" in focus
    assert "Add something" in focus and "Add something" in detail
    assert "goal-focus-add-menu" not in focus
    assert "goal-add-child" not in detail and "goal-add-task" not in detail
    assert "goal_root_starters" in starters and "goal_root_starters_apply" in starters
    assert "goal_intake_recommend" in intake and "goal_intake_propose" in intake
    assert "goal_ai_proposal" in intake and "Approve and add" in intake
    assert "구조 용어를 고를 필요는 없어요" in intake
    assert "goal_create('overgoal'" in controls


def test_growth_map_numbers_active_leaves_in_recommended_execution_order():
    html = _html()
    script = _script()
    ordering = _function_body(script, "goalExecutionOrder")
    focus = _function_body(script, "renderGoalFocusPanel")
    constellation = _function_body(script, "renderGoalConstellation")

    assert "goalSortedActiveLeaves(scope)" in ordering
    assert "child.type==='overgoal'" in ordering
    assert "rank:index+1" in ordering and "total:" in ordering
    assert "goalExecutionBadge(leaf,executionOrder)" in focus
    assert "execution-order-badge" in constellation
    assert "recommended execution order" in constellation
    assert "structurePrefix" in constellation and "goalTypeLabel(node.type,node)" in constellation
    assert "① = recommended execution order" in html
    assert "① = 추천 실행 순서" in script


def test_leaf_workspace_reveals_sections_only_when_they_have_meaning():
    script = _script()
    outcome = _function_body(script, "goalOutcomeHtml")
    relevance = _function_body(script, "goalRelevanceHasContent")
    focus = _function_body(script, "renderGoalFocusPanel")

    assert "node.status!=='completed'&&!outcomes.length" in outcome
    assert "state.last_reviewed_at" in relevance and "view.due" in relevance
    assert "node.type!=='task'&&completion.percent!=null" in focus
    assert "goalRelevanceHasContent(node)?goalRelevanceHtml(node):''" in focus
    assert "goalAiHasContent" in focus and "goalLeafWorkspaceTabHtml(node)" in focus
    assert "goalOutcomeHtml(node)" not in focus
    assert "Which Leaf actions are active?" not in focus


def test_completed_leaf_lifecycle_uses_receipts_history_and_next_leaf_navigation():
    script = _script()
    visibility = _function_body(script, "goalVisibleInActiveMap")
    tree = _function_body(script, "goalTreeHtml")
    summary = _function_body(script, "goalLeafWorkspaceTabHtml")
    history = _function_body(script, "goalCompletedHistoryHtml")
    completion = _function_body(script, "decideLeafWorkspaceProposal")
    advance = _function_body(script, "refreshAfterLeafCompletion")
    completed_controls = _function_body(script, "bindGoalCompletedLeafControls")

    assert "node.type==='task'&&node.status==='completed'" in visibility
    assert "filter(goalVisibleInActiveMap)" in tree
    assert "Completed Leaf" in summary and "완료된 Leaf" in summary
    assert "Confirmed result" in summary and "What was learned" in summary
    assert "View conversation" in summary and "Reopen this Leaf" in summary
    assert "Completed Leaves" in history and "goal-completed-history-item" in history
    assert "proposal.type==='complete_leaf'" in completion
    assert "closeLeafCoach()" in completion and "refreshAfterLeafCompletion" in completion
    assert "handoffLeafId?goalFind" in advance
    assert "reviewScope.semantic_role!=='project'" in advance
    assert "reviewGoalAgent(reviewScope.id,false)" in advance
    assert "goal_leaf_workspace_reopen(node.id)" in completed_controls


def test_leaf_completion_edits_and_opens_only_the_approved_downstream_handoff():
    html = _html()
    script = _script()
    editor = _function_body(script, "leafWorkspaceProposalEditorHtml")
    edited = _function_body(script, "leafWorkspaceEditedPayload")
    incoming = _function_body(script, "leafWorkspaceIncomingHtml")
    render = _function_body(script, "renderLeafCoach")
    advance = _function_body(script, "refreshAfterLeafCompletion")

    assert 'id="leaf-workspace-incoming"' in html
    for field in ("output_summary", "working_material", "constraints",
                  "unresolved_questions", "suggested_start"):
        assert f'data-proposal-handoff-field="{field}"' in editor
    assert "payload.handoff" in edited and "proposalHandoffField" in edited
    assert "Approved handoff from an earlier Leaf" in incoming
    assert "이전 Leaf에서 승인된 인계" in incoming
    assert "raw conversation" in render and "원문 대화" in render
    assert "handoffLeafId?goalFind" in advance
    assert "completion_handoff&&view.completion_handoff.destination_leaf_id" in script


def test_all_details_remember_their_collapsed_state():
    script = _script()
    bind = _function_body(script, "bindCollapsePrefs")
    hydrate = _function_body(script, "hydrateDurableUiPreferences")

    assert "querySelectorAll('details')" in bind
    assert "detailsOpenPref[key]=d.open" in bind
    assert "ffDetailsOpen" in script
    assert "persistDurableUiPreference('details_open',detailsOpenPref)" in bind
    assert "preferences.details_open" in hydrate
    assert "details[data-collapse-key]" in hydrate
    assert "new MutationObserver" in script
    assert "panel.dataset.collapseScope='goal-'" in script


def test_growth_map_positions_pan_and_zoom_survive_full_app_restart():
    script = _script()
    save = _function_body(script, "saveGoalMapPositions")
    hydrate = _function_body(script, "hydrateDurableUiPreferences")
    bind = _function_body(script, "bindConstellationPanZoom")
    build = _function_body(script, "buildGoalConstellation")

    assert "persistDurableUiPreference('growth_map_layout'" in save
    assert "positions:goalMapPositions" in save
    assert "views:constellationViewState" in save
    assert "preferences.growth_map_layout" in hydrate
    assert "goalMapPositions=(layout.positions" in hydrate
    assert "Object.assign(constellationViewState,layout.views)" in hydrate
    assert "saveGoalMapPositions(true)" in bind
    assert "saveGoalMapPositions()" in bind
    assert "const saved=goalMapPositions[item.node.id]" in build


def test_soul_calibration_explains_that_every_path_is_optional():
    script = _script()
    render = _function_body(script, "renderSoulCalDrawer")
    bind = _function_body(script, "bindSoulCalDrawer")

    assert "Every question here is optional." in render
    assert "Partial, vague, or uncertain answers are welcome" in render
    assert "reach the same understanding over time" in render
    assert "Talk to the chatbot instead" in render
    assert "이 질문들은 모두 선택 사항이에요." in render
    assert "대신 챗봇과 대화하기" in render
    assert "soul-cal-chat" in bind and "activateView('self')" in bind


def test_soul_calibration_plain_enter_remains_multiline():
    script = _script()
    bind_calibration = _function_body(script, "bindSoulCalDrawer")
    render = _function_body(script, "renderSoulCalDrawer")
    generic_submit = _function_body(script, "bindShiftEnterSubmit")
    command_center = _function_body(script, "bindCommandCenter")

    assert '<textarea id="soul-cal-answer"' in render
    assert "bindShiftEnterSubmit(textarea" not in bind_calibration
    assert "e.ctrlKey||e.metaKey" in bind_calibration
    assert "!e.isComposing" in bind_calibration

    # The rest of the app retains its existing Enter-to-submit behavior.
    assert "e.key==='Enter' && !e.shiftKey" in generic_submit
    assert "e.preventDefault()" in generic_submit
    assert "bindShiftEnterSubmit(input, sendCommandMessage)" in command_center


def test_soul_calibration_sections_follow_the_active_question():
    checklist = _function_body(_script(), "soulCalChecklistHtml")

    assert '<details class="soul-calibration-section' in checklist
    assert 'data-cal-section="' in checklist
    assert "const activeSection=active&&active.section" in checklist
    assert "sec.section===activeSection?' open':''" in checklist
    assert "covered===attrs.length" in checklist


def test_documents_attach_to_calibration_and_investigation_context():
    script = _script()
    calibration = _function_body(script, "renderSoulCalDrawer")
    question = _function_body(script, "curQuestionHtml")
    card = _function_body(script, "curCardHtml")
    binder = _function_body(script, "bindContextDocuments")

    assert "active.attachments" in calibration
    assert "active.attachment_key" in calibration
    assert "item.context_attachments" in question
    assert "curDocumentContextHtml(cur)" in card
    assert "context_attachment_add" in binder
    assert "context_attachment_remove" in binder
    assert "setRangeText" in binder
