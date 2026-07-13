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
    assert "goal_experiment_outcome" in bind
    assert "lower-confidence interpretation is ready for review" in bind
    assert "Record what happened so Faerie can learn" in focus


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
    assert "goal-focus-draft-steps" in starter
    assert "goalNodeNeedsDefinition(node)" in render
    assert render.find("goalNodeNeedsDefinition(node)") < render.find("goalRecapHtml(")
    assert "return;" in render[render.find("goalNodeNeedsDefinition(node)"):render.find("goalRecapHtml(")]


def test_goalai_step_drafts_are_accented_and_survive_panel_rerenders():
    script = _script()
    leaf_steps = _function_body(script, "goalLeafStepsHtml")
    starter = _function_body(script, "goalDefinitionStarterHtml")
    draft = _function_body(script, "draftLeafSteps")
    persisted = _function_body(script, "persistGoalStepDraft")
    draft_html = _function_body(script, "goalStepDraftHtml")
    bind = _function_body(script, "bindGoalStepDraft")

    assert 'class="accent" id="goal-focus-draft-steps"' in leaf_steps
    assert 'class="accent" id="goal-focus-draft-steps"' in starter
    assert "ffGoalStepDrafts" in script
    assert "localStorage.setItem('ffGoalStepDrafts'" in persisted
    assert "goal_leaf_step_draft(node.id)" in draft
    assert "persistGoalStepDraft(node.id,text,draft)" in draft
    assert "goal_ai_chat(node.id" in draft
    assert "renderGoalFocusPanel()" in draft
    assert "goalStepDraftHtml(node)" in leaf_steps and "goalStepDraftHtml(node)" in starter
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


def test_leaf_step_coach_has_both_entry_points_and_bounded_sidebar():
    html = _html()
    script = _script()
    steps = _function_body(script, "goalLeafStepsHtml")
    open_coach = _function_body(script, "openLeafCoach")
    render_coach = _function_body(script, "renderLeafCoach")
    bind = _function_body(script, "bindGoalFocusPanel")

    assert 'id="leaf-step-coach"' in html
    assert 'id="goal-step-help-all"' in steps
    assert 'data-step-help="' in steps
    assert "Get help with these steps" in steps
    assert "event.preventDefault(); event.stopPropagation();" in bind
    assert "goal_step_coach_open" in open_coach
    assert "cannot see other Branches, global memory, the main chat, or screen activity" in render_coach
    assert "다른 Branch, 전체 기억, 메인 채팅 또는 화면 활동은 볼 수 없어요" in render_coach
    assert "goal_step_coach_set_status" in bind


def test_leaf_step_coach_renders_examples_retry_shortcuts_and_clear():
    html = _html()
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")
    error = _function_body(script, "leafCoachSetError")

    assert "leaf-coach-example" in message
    assert "payload.next_action" in message and "payload.question" in message
    assert 'data-coach-shortcut="example"' in html
    assert 'data-coach-shortcut="smaller"' in html
    assert 'data-coach-shortcut="stuck"' in html
    assert "leaf-coach-retry" in error
    assert "goal_step_coach_clear" in script


def test_leaf_coach_offers_responses_and_reviewable_step_revisions():
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")
    decision = _function_body(script, "decideLeafCoachRevision")

    assert "Suggested responses" in message and "제안된 응답" in message
    assert "Proposed change to How to do this" in message
    assert "leaf-coach-revision-steps" in message
    assert "goal_step_coach_revision" in decision
    assert "edited" not in decision.lower() or "steps" in decision


def test_text_is_selectable_and_right_click_has_copy_paste_menu():
    html = _html()
    script = _script()

    assert "user-select:text" in html
    assert ".text-context-menu" in html
    assert "installTextContextMenu" in script
    assert "clipboard_write" in script and "clipboard_read" in script
    assert "contextmenu" in script
    assert "text_select=True" in (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "Confirmed step resolutions will remain" in script


def test_leaf_coach_close_stays_fixed_and_uses_an_accessible_x():
    html = _html()

    assert 'class="cur-head leaf-coach-head"' in html
    assert 'class="leaf-coach-scroll"' in html
    assert 'aria-label="Close Leaf Coach"' in html
    assert '>×</button>' in html
    assert ".leaf-coach-drawer.open { display:flex; flex-direction:column; }" in html
    assert ".leaf-coach-scroll { flex:1 1 auto; min-height:0; overflow-y:auto;" in html


def test_leaf_coach_confirms_completion_then_advances_to_next_step():
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")
    confirm = _function_body(script, "confirmLeafCoachCompletion")

    assert "Should I mark it complete?" in message
    assert 'data-coach-complete="yes"' in message
    assert 'data-coach-complete="no"' in message
    assert "goal_step_coach_confirm_completion" in confirm
    assert "step.index>stepIndex" in confirm
    assert "goal_step_coach_open" in confirm
    assert "Beginning step " in confirm
    assert "단계가 완료된 것 같아요. 완료로 표시할까요?" in message


def test_leaf_coach_confirmed_completion_updates_the_matching_checkbox():
    script = _script()
    sync = _function_body(script, "syncLeafCoachStepCompletion")
    send = _function_body(script, "sendLeafCoachMessage")

    assert "item.status==='completed'?'1':'0'" in sync
    assert "goalStepKey(node.id,steps[item.index])" in sync
    assert "renderGoalFocusPanel()" in sync
    assert "syncLeafCoachStepCompletion(leafCoachView)" in send


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
    assert "Nothing is deleted or recreated" in manual
    assert "The same node ID will be preserved" in manual
    assert "Create proposal for approval" in manual
    assert "The tree has not changed" in manual
    assert "구조 안전하게 변경" in manual
    assert "같은 노드 ID가 유지됩니다" in manual
    assert "restructure_node" in proposal and "restructure_tree" in proposal
    assert "Restructure without losing data" in proposal
    assert "Whole-path restructure" in proposal
    assert "Approve restructure" in script
    assert "goal-restructure-open" in detail
    assert "goal-focus-restructure" in focus
    assert "goal-focus-restructure-panel" in focus
    assert "goal-focus-ask-command" in focus and "Restructure" in focus
    assert "renderGoalRestructurePanel(node,$('goal-focus-restructure-panel'))" in bind_focus
    assert "#goal-focus-panel .agent-proposal[data-pid]" in bind_focus


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
    assert "◇ Area / Project / Stage" in html
    assert "◇ 영역 / 프로젝트 / 단계" in script


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
    assert "goalAiHasContent" in focus and "node.status==='completed'" in focus
    assert "Which Leaf actions are active?" not in focus


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
