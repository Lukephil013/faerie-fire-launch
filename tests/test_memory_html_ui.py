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


def test_command_center_has_guarded_browser_task_cards_and_domain_controls():
    html = _html()
    script = _script()
    render = _function_body(script, "renderBrowserTasks")
    buttons = _function_body(script, "browserTaskButtons")
    actions = _function_body(script, "runBrowserTaskAction")
    permissions = _function_body(script, "renderBrowserPermissions")

    assert 'id="cc-browser-tasks"' in html
    assert 'id="settings-browser-domains"' in html
    assert "browserTaskButtons(task)" in render
    assert "review_ready" in buttons and "Fill these fields" in buttons
    assert "buttons.push(['cancel'" in buttons
    assert "command_browser_approve_domain" in actions
    assert "command_browser_scan" in actions
    assert "command_browser_fill" in actions
    assert "command_browser_finish" in actions
    assert "command_browser_revoke" in script
    assert "data-browser-revoke" in permissions
    assert "save manually" in script


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
    synthesize = _function_body(script, "synthesizeCuriosity")

    assert "cur.synthesis_due" in render
    assert "new experiment outcome" in render
    assert "Previous approved interpretation" in render
    assert "Approve edited interpretation" in render
    assert "Review with new evidence" in render
    assert "curiosity_synthesize" in synthesize
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
    assert "Look for worthwhile Investigations" not in render
    assert "curiosity_candidate_suggest" not in bind
    assert "curiosity_candidate_action" in bind
    assert "candidate-start" in bind
    assert "sensitive topic" in bind
    assert "Starting Investigation" in bind


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


def test_investigation_workspace_has_overview_shelf_and_focused_resumable_session():
    html = _html()
    script = _script()
    overview = _function_body(script, "curOverviewHtml")
    session = _function_body(script, "renderCuriositySession")

    assert 'class="curiosity-shell"' in html
    assert 'id="cur-workspace"' in html and 'id="cur-shelf"' in html
    assert 'id="cur-session"' in html and 'id="cur-session-content"' in html
    assert "Recommended next step" in overview and "Investigation at a glance" in overview
    assert "Investigation shelf" in script and "Switch investigations without losing your place" in script
    assert "Save & continue" in session and "Exit session" in session
    assert "curDraft:" in session and "contextDocumentHtml" in session
    assert "curiosity_answer" in session
    assert "ffCuriositySession" in script and "ffCuriosityQueue" in script


def test_investigation_understanding_review_is_synthesis_only():
    script = _script()
    review = _function_body(script, "curUnderstandingReviewHtml")
    open_details = _function_body(script, "openCuriosityDetails")
    render = _function_body(script, "renderCuriosity")

    assert "Current working interpretation" in review
    assert "Run a synthesis for this Investigation" in review
    assert "Previous approved interpretation" in review
    assert "Updated interpretation" in review and "Previous interpretation" in review
    assert "What Faerie knows so far" in review
    assert "What remains unknown" in review
    assert "Exceptions or counterevidence" in review
    assert "Possible next experiments" in review
    assert "curLoopHtml" not in review and "curThreadsHtml" not in review
    assert "curPersonModelHtml" not in review
    assert "curiosityDetailsMode=mode" in open_details
    assert "function openCuriosityDetails(focusSelector,mode='understanding')" in script
    assert "understandingOnly?curUnderstandingReviewHtml" in render
    assert "focusedManagement" in render
    assert "placementOnly?curPlacementReviewHtml" in render
    assert "curThreadsHtml(selected)" not in render and "curLoopHtml(selected)" not in render


def test_investigation_sessions_show_progress_proposals_and_advance_the_queue():
    script = _script()
    progress = _function_body(script, "curSessionUnderstandingProgressHtml")
    finish = _function_body(script, "exitCuriositySession")
    step = _function_body(script, "curRecommendedStep")
    render = _function_body(script, "renderCuriosity")

    assert "Estimated understanding" in progress and "Synthesis confidence" in progress
    # Rapid-fire model: progress counts down to a synthesize handoff, not proposals.
    assert "until Faerie can synthesize" in progress
    assert "ready to synthesize" in progress
    assert "proactive proposal checkpoint" not in progress
    assert "curQueuedIds" in finish and "nextQueued" in finish
    assert "Continuing with the next queued Investigation" in finish
    # Auto-continue keeps asking until the understanding target, then stops.
    assert "CUR_SYNTHESIS_ANSWER_TARGET" in finish and "continueCuriosity" in finish
    assert "keepAsking" in finish
    assert "kind:'proposals'" in step
    assert step.index("if(suggestions)") < step.index("if(questions)")
    assert "curProposalReviewHtml" in render and "bindCuriosityProposals" in render


def test_investigation_session_forces_repaint_after_answer_and_on_exit():
    """The Save button spins as 'Saving answer' until loadCuriosity repaints;
    both the answer path and the exit path must force the reload so an in-flight
    load's guard can't swallow the repaint (the 'hangs until I switch tabs' bug)."""
    script = _script()
    session = _function_body(script, "renderCuriositySession")
    finish = _function_body(script, "exitCuriositySession")

    assert "loadCuriosity({force:true})" in session
    assert "loadCuriosity({force:true})" in finish
    # A plain reload on these paths is what regressed into the hang.
    assert "saveCuriositySession();loadCuriosity();" not in session


def test_investigation_batch_keeps_answered_questions_so_it_does_not_end_early():
    """makeCuriositySession must keep already-answered questions in the batch so
    the step index stays aligned and the batch isn't declared complete after a
    single answer (which skipped the remaining queued questions)."""
    node = shutil.which("node")
    if not node:
        return
    body = _function_body(_script(), "makeCuriositySession")
    harness = (
        "let curiositySession=null;\n"
        "let curQuickMode=false;\n"
        "const CUR_QUICK_TARGET=6;\n"
        "function loadSavedCuriositySession(){return curiositySession;}\n"
        "function saveCuriositySession(){}\n"
        "function makeCuriositySession(cur){" + body + "}\n"
        "const C=7;\n"
        "const two=[{id:1},{id:2}];\n"
        # Fresh session: both questions form the batch.
        "const fresh=makeCuriositySession({id:C,open_questions:two,resolved:[]});\n"
        # Simulate having answered Q1 (submit advances the index).
        "curiositySession={curiosityId:C,batch:[1,2],currentIndex:1,answered:1};\n"
        "const midCur={id:C,open_questions:[{id:2}],"
        "resolved:[{id:1,kind:'question',status:'answered'}]};\n"
        "const mid=makeCuriositySession(midCur);\n"
        # Simulate having answered Q2 as well.
        "curiositySession={curiosityId:C,batch:[1,2],currentIndex:2,answered:2};\n"
        "const doneCur={id:C,open_questions:[],resolved:["
        "{id:1,kind:'question',status:'answered'},"
        "{id:2,kind:'question',status:'answered'}]};\n"
        "const done=makeCuriositySession(doneCur);\n"
        "console.log(JSON.stringify({fresh:fresh.batch,mid:mid.batch,"
        "midIndex:mid.currentIndex,done:done.batch}));\n"
    )
    result = subprocess.run(
        [node, "-e", harness], text=True, encoding="utf-8",
        cwd=ROOT, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    import json
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # Fresh batch holds both questions.
    assert out["fresh"] == [1, 2]
    # After answering Q1, Q1 stays in the batch and the index still points at Q2
    # (index 1) — the batch is NOT truncated to just [2], which is what ended the
    # session early before the fix.
    assert out["mid"] == [1, 2]
    assert out["midIndex"] == 1
    # Once every question is answered the reused batch has no open question left,
    # so it falls through to an empty fresh batch and the session completes.
    assert out["done"] == []


def test_investigation_primary_actions_preserve_existing_engine_and_management_controls():
    script = _script()
    begin = _function_body(script, "beginCuriositySession")
    bind = _function_body(script, "bindCuriosityOverview")
    render = _function_body(script, "renderCuriosity")

    assert "continueCuriosity" in begin
    assert "curiosity_set" in bind
    assert "curiosity_reactivate" in bind
    assert "bindCandidatePanel" in render
    assert "bindRelatedInvestigationPanel" in render
    assert "bindCuriosityUnderstanding" in render
    assert "bindCuriosityPlacement" in render
    assert "Review current understanding" in script and "현재 이해 검토" in script


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
    assert '<option value="tree">Living Tree</option>' in html
    assert '<option value="solar">Solar System</option>' in html
    assert "ffGrowthMapSkin" in script
    assert "localStorage.setItem('ffGrowthMapSkin'" in setting
    assert "classList.toggle('skin-solar'" in setting
    assert "box.classList.toggle('skin-solar'" in constellation
    assert "solar-orbit" in constellation
    assert "solarSun" in constellation and "solarPlanet" in constellation
    assert "planet-shine" in constellation
    assert "rings" in layout
    assert ".growth-map-tree.skin-solar" in html
    assert "'Growth Map Style':'성장 지도 스타일'" in script
    assert "'Solar System':'태양계'" in script


def test_unsaved_leaves_spawn_in_a_compact_ring_around_their_parent():
    script = _script()
    layout = _function_body(script, "buildGoalConstellation")
    compact = _function_body(script, "placeGoalLeavesNearParents")

    assert "nodes.push({node,depth,x,y,baseX:x,baseY:y,angle,parentId})" in layout
    assert "placeGoalLeavesNearParents(nodes,width,height)" in layout
    assert "item.node.type!=='task'" in compact
    assert "manuallyPlaced" in compact
    assert "const distance=76+Math.floor(offset/perRing)*38" in compact
    assert "x=parent.x+Math.cos(angle)*distance" in compact
    assert "y=parent.y+Math.sin(angle)*distance" in compact


def test_planning_roles_mark_a_single_order_based_focus_path():
    """goalDerivePlanningRoles follows the lowest-position active child at each
    level to a single FOCUS Project and marks its one open Leaf as NOW."""
    node = shutil.which("node")
    if not node:
        return
    body = _function_body(_script(), "goalDerivePlanningRoles")
    harness = (
        "function goalIsProject(n){return !!(n&&n.type==='subgoal'&&n.semantic_role==='project');}\n"
        "function goalProjectFocus(n){return (n&&n.project_focus)||{};}\n"
        "function goalDerivePlanningRoles(node){" + body + "}\n"
        "const leaf1={id:100,type:'task',status:'active',position:0,children:[]};\n"
        "const leaf2={id:101,type:'task',status:'active',position:1,children:[]};\n"
        "const projA={id:30,type:'subgoal',semantic_role:'project',status:'active',position:0,"
        "project_focus:{},children:[leaf1,leaf2]};\n"
        "const projB={id:31,type:'subgoal',semantic_role:'project',status:'active',position:1,"
        "project_focus:{},children:[]};\n"
        "const branch={id:20,type:'subgoal',semantic_role:'area',status:'active',position:0,"
        "children:[projA,projB]};\n"
        "const root1={id:10,type:'overgoal',status:'active',position:0,children:[branch]};\n"
        "const root2={id:11,type:'overgoal',status:'active',position:1,children:[]};\n"
        "const soul={id:1,type:'umbrella',children:[root1,root2]};\n"
        "goalDerivePlanningRoles(soul);\n"
        "console.log(JSON.stringify({focusA:!!projA.project_focus.focus,"
        "focusB:!!projB.project_focus.focus,now1:leaf1.planning_role,now2:leaf2.planning_role}));\n"
    )
    result = subprocess.run(
        [node, "-e", harness], text=True, encoding="utf-8",
        cwd=ROOT, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    import json
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["focusA"] is True and out["focusB"] is False
    assert out["now1"] == "now"
    assert out["now2"] in (None, "", "null") or out["now2"] is None


def test_investigations_group_into_completion_tiers():
    """The shelf groups investigations into completion tiers by understanding %:
    not started / exploring / developing / ready(90%+) / synthesized."""
    node = shutil.which("node")
    if not node:
        return
    script = _script()
    pct = _function_body(script, "curUnderstandingPct")
    tier = _function_body(script, "curCompletionTier")

    # The shelf renders one section per tier (most complete first), not tabs.
    shelf = _function_body(script, "curShelfHtml")
    assert "CUR_COMPLETION_TIERS" in script
    assert "curCompletionTier" in shelf and "cur-shelf-tier-head" in shelf
    assert "tierOrder=['synthesized','ready','developing','early','notstarted']" in shelf

    harness = (
        "function curUnderstandingPct(cur){" + pct + "}\n"
        "function curCompletionTier(cur){" + tier + "}\n"
        "const C=(answered,syntheses)=>({item_counts:{answered},syntheses:syntheses||[]});\n"
        "console.log(JSON.stringify({\n"
        "  notstarted:curCompletionTier(C(0)),\n"
        "  early:curCompletionTier(C(5)),\n"          # 25%
        "  developing:curCompletionTier(C(11)),\n"    # 55%
        "  ready:curCompletionTier(C(18)),\n"         # 90%
        "  synthesized:curCompletionTier(C(4,[{status:'approved',payload:{confidence:0.4}}])),\n"
        "}));\n"
    )
    result = subprocess.run(
        [node, "-e", harness], text=True, encoding="utf-8",
        cwd=ROOT, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    import json
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {"notstarted": "notstarted", "early": "early",
                   "developing": "developing", "ready": "ready",
                   "synthesized": "synthesized"}


def test_investigation_proposals_ui_is_gated_off():
    """P1c: the proposal review panel, its recommended-step entry, and inline
    suggestion sections are all gated behind CUR_PROPOSALS_UI_ENABLED (false),
    so no proposals surface in the Investigations UI."""
    script = _script()
    assert "const CUR_PROPOSALS_UI_ENABLED=false" in script
    review = _function_body(script, "curProposalReviewHtml")
    step = _function_body(script, "curRecommendedStep")
    card = _function_body(script, "curCardHtml")

    assert "if(!CUR_PROPOSALS_UI_ENABLED) return ''" in review
    assert "CUR_PROPOSALS_UI_ENABLED?(cur.open_suggestions||[]).length:0" in step
    assert "CUR_PROPOSALS_UI_ENABLED?(cur.open_suggestions||[]).filter" in card


def test_synthesis_hands_off_to_the_main_chat():
    """At ~90% understanding the session offers 'Synthesize & discuss in main
    chat', which synthesizes, switches to the Command Center, and posts an
    analysis prompt so the main chat restates and analyzes the investigation."""
    script = _script()
    session = _function_body(script, "renderCuriositySession")
    handoff = _function_body(script, "synthesizeAndHandoff")
    post = _function_body(script, "sendCommandPrompt")

    # The button appears only once understanding is reached.
    assert "cur-session-synthesize" in session
    assert "answered>=CUR_SYNTHESIS_ANSWER_TARGET||syntheses>0" in session
    # Handoff: synthesize → leave investigation → main chat → seed analysis.
    assert "curiosity_synthesize" in handoff
    assert "switchView('self')" in handoff
    assert "sendCommandPrompt" in handoff
    assert "give me your own analysis" in handoff
    # The post helper reaches the main chat send path / API.
    assert "sendCommandMessage" in post and "command_send" in post


def test_focus_refresh_preserves_an_open_restructure_proposal():
    """Alt-tabbing away and back must not wipe an in-review restructure/intake
    panel: the window focus handler skips loadGoals() while one is open."""
    script = _script()
    guard = _function_body(script, "goalDetailHasOpenEditor")

    assert "goal-restructure-panel" in guard
    assert ".goal-intake-panel" in guard and ".goal-root-starters-panel" in guard
    # It also treats an unsaved node-detail field edit as an open editor.
    assert "goalDetailHasUnsavedEdits()" in guard
    # The window focus handler gates its growth reload on the guard, so an
    # alt-tab no longer wipes an open proposal.
    focus = script[script.index("window.addEventListener('focus'"):]
    focus = focus[:focus.index("});") + 3]
    assert "goalState && !goalDetailHasOpenEditor()" in focus
    assert "if(goalState) loadGoals();" not in focus


def test_unsaved_node_detail_edits_block_the_focus_refresh():
    """A half-typed Title/Description/Notes edit counts as an open editor so
    the focus refresh won't revert it."""
    node = shutil.which("node")
    if not node:
        return
    script = _script()
    find = _function_body(script, "goalFind")
    dirty = _function_body(script, "goalDetailHasUnsavedEdits")

    harness = (
        "function goalFind(node,id){" + find + "}\n"
        "const fields={};\n"
        "function $(id){return fields[id]||null;}\n"
        "let goalState={tree:{id:1,title:'T',description:'D',notes:'N',"
        "status:'active',priority:'normal',due_date:'',children:[]}};\n"
        "let selectedGoalId=1;\n"
        "function goalDetailHasUnsavedEdits(){" + dirty + "}\n"
        # Fields all match the saved node → clean.
        "fields['goal-title']={value:'T'};fields['goal-description']={value:'D'};\n"
        "fields['goal-notes']={value:'N'};fields['goal-node-status']={value:'active'};\n"
        "fields['goal-priority']={value:'normal'};fields['goal-due']={value:''};\n"
        "const clean=goalDetailHasUnsavedEdits();\n"
        # User types into Description → dirty.
        "fields['goal-description']={value:'D and more'};\n"
        "const edited=goalDetailHasUnsavedEdits();\n"
        "console.log(JSON.stringify({clean,edited}));\n"
    )
    result = subprocess.run(
        [node, "-e", harness], text=True, encoding="utf-8",
        cwd=ROOT, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    import json
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["clean"] is False
    assert out["edited"] is True


def test_unsaved_nodes_follow_the_nearest_dragged_ancestor():
    """Freshly generated Projects/Branches (no saved position yet) should ride
    along with the nearest ancestor the user dragged, so they spawn beside their
    parent instead of at the default center-relative slot."""
    node = shutil.which("node")
    if not node:
        return
    layout = _function_body(_script(), "buildGoalConstellation")
    follow = _function_body(_script(), "placeGoalNodesNearMovedAncestor")

    # It runs before leaves are placed, and only shifts unsaved non-leaf nodes.
    assert "placeGoalNodesNearMovedAncestor(nodes,width,height)" in layout
    assert "item.hasSaved=true" in layout
    assert "item.node.type==='task'" in follow  # tasks are skipped (leaves handled elsewhere)

    harness = (
        "function placeGoalNodesNearMovedAncestor(nodes,width,height){" + follow + "}\n"
        # root (moved), area (moved via saved), project (fresh, no saved pos)
        "const root={node:{id:1,type:'overgoal'},parentId:null,baseX:100,baseY:100,x:100,y:100,hasSaved:false};\n"
        "const area={node:{id:2,type:'subgoal'},parentId:1,baseX:150,baseY:150,x:400,y:360,hasSaved:true};\n"
        "const proj={node:{id:3,type:'subgoal'},parentId:2,baseX:180,baseY:180,x:180,y:180,hasSaved:false};\n"
        "const leaf={node:{id:4,type:'task'},parentId:3,baseX:200,baseY:200,x:200,y:200,hasSaved:false};\n"
        "const nodes=[root,area,proj,leaf];\n"
        "placeGoalNodesNearMovedAncestor(nodes,1100,720);\n"
        "console.log(JSON.stringify({projX:proj.x,projY:proj.y,leafX:leaf.x,leafY:leaf.y}));\n"
    )
    result = subprocess.run(
        [node, "-e", harness], text=True, encoding="utf-8",
        cwd=ROOT, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    import json
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # Area was dragged by (+250,+210); the fresh Project follows by the same
    # delta: 180+250=430, 180+210=390.
    assert out["projX"] == 430 and out["projY"] == 390
    # The Leaf is a task — left untouched here (placeGoalLeavesNearParents owns it).
    assert out["leafX"] == 200 and out["leafY"] == 200


def test_sibling_projects_get_numbered_order_badges():
    """Multiple sibling Projects get 1..N order badges; a lone Project gets none."""
    node = shutil.which("node")
    if not node:
        return
    script = _script()
    seq = _function_body(script, "goalProjectSequenceMap")
    render = _function_body(script, "renderGoalConstellation")

    assert "const projectSeq=goalProjectSequenceMap(tree)" in render
    assert "project-seq-badge" in render

    harness = (
        "function goalVisibleChildren(node){return (node&&node.children)||[];}\n"
        "function goalIsProject(node){return !!(node&&node.type==='subgoal'&&node.semantic_role==='project');}\n"
        "function goalProjectSequenceMap(tree){" + seq + "}\n"
        "const P=(id)=>({id,type:'subgoal',semantic_role:'project',children:[]});\n"
        "const area={id:10,type:'subgoal',semantic_role:'area',children:[P(11),P(12),P(13)]};\n"
        "const loneArea={id:20,type:'subgoal',semantic_role:'area',children:[P(21)]};\n"
        "const tree={id:1,type:'umbrella',children:[area,loneArea]};\n"
        "console.log(JSON.stringify(goalProjectSequenceMap(tree)));\n"
    )
    result = subprocess.run(
        [node, "-e", harness], text=True, encoding="utf-8",
        cwd=ROOT, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    import json
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # Order-based model: sibling Projects are numbered 1..N; the lone Project is
    # not numbered. The focus Project shows FOCUS instead (rendered separately).
    assert out == {"11": "1", "12": "2", "13": "3"}


def test_growth_map_is_viewport_locked_compact_and_gold_framed():
    html = _html()

    assert "#view-goals { height:100vh; box-sizing:border-box; overflow:hidden; padding:0 4px 4px; }" in html
    assert 'id="goal-ai-strip"' not in html
    assert ".growth-map-main { height:100%; min-height:0; overflow:hidden; }" in html
    assert "gap:4px; align-items:stretch; height:100%; min-height:0" in html
    assert ".growth-map-main .growth-map-tree { flex:1 1 auto; height:auto; min-height:0;" in html
    assert 'border-image:url("assets/icons/golden-trim.png") 140 155 140 155 fill / 14px' in html
    assert ".goal-focus-panel { position:relative; top:0; height:100%; max-height:none; overflow:auto;" in html
    assert ".growth-map-main .constellation-legend { position:absolute; z-index:8; top:17px; left:17px; right:17px;" in html
    assert "box.classList.add('dragging','panning-all')" in html
    assert "box.classList.remove('dragging','panning-all')" in html


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

    assert "nav .tab .nav-icon" in html
    assert html.count('class="nav-icon"') >= 3
    assert "border-radius:7px" in html
    assert "background:rgba(23,28,25,.94)" in html
    assert "box-shadow:inset 2px 0 0 var(--green)" in html


def test_command_center_rail_cards_share_the_portrait_gold_frame():
    html = _html()

    assert "header > .self-panel.self-identity-card" in html
    assert "#self-profile-widgets > .command-widget" in html
    assert 'border-image:url("assets/icons/golden-trim.png") 140 155 140 155 fill / 14px' in html


def test_command_center_rail_lifts_profile_and_compacts_gold_cards():
    html = _html()

    assert "header { position:relative" in html
    assert "header .brand { position:absolute" in html
    assert "top:15px; right:20px" in html
    assert "gap:0; padding:4px 9px 2px" in html
    assert "#self-rail-dashboard { display:flex; flex-direction:column; gap:4px; margin:6px 0 4px; }" in html


def test_primary_navigation_lives_inside_the_gold_profile_container():
    html = _html()
    script = _script()
    header = html[html.index("<header>"):html.index("</header>")]

    assert 'class="rail-primary-nav"' in header
    assert header.count('class="nav-icon"') == 3
    assert 'data-view="self"' in header
    assert 'data-view="goals"' in header
    assert 'data-view="curiosity"' in header
    assert 'id="self-portrait"' in header
    assert header.index('id="self-level-widget"') < header.index('id="self-portrait"') < header.index('class="rail-primary-nav"')
    assert ".rail-primary-nav { display:grid; gap:2px" in html
    assert "nav .tab,.rail-primary-nav .tab" in html
    assert "querySelectorAll('nav .tab,.rail-primary-nav .tab')" in script


def test_shared_sidebar_is_persistent_and_collapsible_across_primary_views():
    html = _html()
    script = _script()
    activate = _function_body(script, "activateView")

    assert 'id="rail-collapse"' in html
    assert "body.rail-collapsed #app" in html
    assert "ffRailCollapsed" in script
    assert "if(railDash) railDash.style.display=''" in activate
    assert "Collapse sidebar" in script and "사이드바 접기" in script


def test_portrait_customizer_stays_bounded_inside_compact_rail_portrait():
    html = _html()

    assert ".self-customizer { position:absolute; z-index:12; top:44px; right:8px" in html
    assert "max-height:calc(100% - 52px); overflow-y:auto; box-sizing:border-box" in html
    assert ".self-customizer .actions button { padding:6px 7px; font-size:10.5px; white-space:nowrap; }" in html


def test_command_center_rail_uses_large_nav_icons_and_keeps_today_first():
    html = _html()
    start = html.index("function selfProfileWidgetsHtml(data)")
    end = html.index("function bindSelfDashboard()", start)
    render = html[start:end]

    assert "width:30px; height:30px" in html
    assert render.index("command-widget-today") < render.index("command-widget-threads")
    assert "command-widget-state" not in render
    assert "command-widget-milestone" not in render
    assert "Current State" not in render
    assert "Next Milestone" not in render


def test_today_widget_keeps_only_completions_from_the_current_local_day():
    script = _script()
    completed = _function_body(script, "selfCompletedToday")
    render = _function_body(script, "selfProfileWidgetsHtml")

    assert "task.completed_at" in completed
    assert "selfLocalDateKey(task.completed_at)===selfLocalDateKey(now||new Date())" in completed
    assert "tasks.filter(t=>selfCompletedToday(t))" in render
    assert "tasks.filter(t=>t.status==='completed')" not in render
    assert "selfDashboardDate" in script and "},60000);" in script


def test_command_center_level_bar_uses_python_computed_soul_level():
    html = _html()
    start = html.index("function selfLevelWidgetHtml(data)")
    end = html.index("function selfProfileWidgetsHtml(data)", start)
    render = html[start:end]

    assert "data.curiosity&&data.curiosity.global_xp" in render
    # Level/XP come from Python (the easy-until-100 curve) so the dashboard and
    # the level-up toast agree; the old flat /100 math is only a fallback.
    assert "cur.soul_level" in render
    assert "cur.soul_xp_into_level" in render
    assert "cur.soul_level_span" in render
    assert "self-xp-bar" in render
    assert "xpIntoLevel+' / '+span+' XP" in render
    # Fallback to the old math is retained if the fields are missing.
    assert "Math.floor(totalXp/100)+1" in render


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
    assert "button,nav .tab,.rail-primary-nav .tab,.group-label" in script
    assert "},140)" in show


def test_leaf_workspace_renders_optional_suggestions_retry_and_clear():
    html = _html()
    script = _script()
    message = _function_body(script, "leafCoachMessageHtml")
    error = _function_body(script, "leafCoachSetError")
    bind = _function_body(script, "leafWorkspaceBindActions")

    assert "leaf-workspace-suggestion" in message
    assert "message.content||payload.content||payload.text" in message
    assert "payload.next_action" not in message and "payload.question" not in message
    assert "data-coach-shortcut" not in html
    assert "leaf-coach-retry" in error
    assert "payload.recovered_partial===true" in message
    assert "Regenerate full response" in message
    assert "data-retry-partial" in message
    assert "kind:'retry_partial_response'" in bind
    assert "Regenerating the full response" in bind
    assert "goal_leaf_workspace_clear" in script


def test_command_center_frame_sits_flush_to_top_and_near_right_edge():
    html = _html()

    assert "#view-self { height:100vh; box-sizing:border-box; overflow:hidden; padding:0 4px 0 0; }" in html


def test_command_center_is_viewport_locked_without_investigation_strip():
    html = _html()

    assert "command-investigations-panel" not in html
    assert 'id="self-investigations-cards"' not in html
    assert ".command-chat-col { display:flex; flex-direction:column; gap:0; height:100%; min-height:0;" in html
    assert ".command-chat-panel { flex:1 1 auto; height:100%; min-height:0;" in html
    assert ".cc-log { flex:1; min-height:300px; overflow:auto;" in html


def test_command_center_uses_distinct_compact_bubbles_for_both_sides():
    html = _html()
    script = _script()
    compact = _function_body(script, "commandConversationHtml")
    render = _function_body(script, "renderCommandChat")

    assert ".cc-message.assistant { align-self:flex-start; max-width:88%;" in html
    assert "background:linear-gradient(135deg,rgba(86,82,164,.24),rgba(48,72,112,.22));" in html
    assert ".cc-message.user { align-self:flex-end; background:rgba(47,227,160,.12);" in html
    assert ".cc-message p + p { margin-top:8px; }" in html
    assert "line-height:1.45" in html
    assert "split(/\\n[ \\t]*\\n+/)" in compact
    assert "commandConversationHtml(commandMessageText(m))" in render


def test_command_center_renders_copy_ready_text_without_literal_quote_arrows():
    html = _html()
    script = _script()
    compact = _function_body(script, "commandConversationHtml")
    render = _function_body(script, "renderCommandChat")
    bind = _function_body(script, "bindCommandCopyBlocks")

    assert ".cc-copy-block" in html
    assert "Copy-ready text" in compact
    assert "lines.some(line=>/^\\s*>/.test(line)||copyLine(line))" in compact
    assert "copyLine=line=>line.trim().match" in compact
    assert "replace(/^\\s*>\\s?/,''" in compact
    assert "flushPlain(); quoteLines.push" in compact
    assert "rendered+=copyBlock(standalone[1])" in compact
    assert "split(/```" in compact
    assert "data-cc-copy-block" in compact
    assert "clipboard_write" in bind and "navigator.clipboard.writeText" in bind
    assert "result&&result.ok!==false" in bind
    assert "document.execCommand('copy')" in bind
    assert "bindCommandCopyBlocks(log)" in render


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
    gui = (ROOT / "gui.py").read_text(encoding="utf-8")

    assert "user-select:text" in html
    assert ".text-context-menu" in html
    assert "installTextContextMenu" in script
    assert "clipboard_write" in script and "clipboard_read" in script
    assert "contextmenu" in script
    assert "text_select=True" in gui
    assert "SetClipboardData(13, handle)" in gui
    assert "GetClipboardData(13)" in gui
    assert "approved handoffs remain" in script


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


def test_leaf_workspace_composer_supports_leaf_scoped_documents():
    html = _html()
    script = _script()
    rendered = _function_body(script, "renderLeafCoach")
    enabled = _function_body(script, "leafWorkspaceSetEnabled")
    refresh = _function_body(script, "refreshLeafWorkspaceAttachments")

    assert 'id="leaf-workspace-attachments"' in html
    assert "contextDocumentHtml" in rendered
    assert "view.attachments||[],'leaf_workspace',view.leaf_id,true" in rendered
    assert "bindContextDocuments" in rendered
    assert "documents attached to this Leaf" in rendered
    assert "another Leaf’s raw conversation or attachments" in rendered
    assert "#leaf-workspace-attachments button" in enabled
    assert "goal_leaf_workspace_open(leafId)" in refresh


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


def test_leaf_workspace_mixed_question_blocks_share_one_submission():
    html = _html()
    script = _script()
    questions = _function_body(script, "leafWorkspaceMessageQuestions")
    question_html = _function_body(script, "leafWorkspaceQuestionSetHtml")
    message = _function_body(script, "leafCoachMessageHtml")
    bind = _function_body(script, "leafWorkspaceBindActions")

    assert "single_choice" in questions and "multi_select" in questions
    assert "question.required!==false" in questions
    assert "inputType=question.type==='single_choice'?'radio':'checkbox'" in question_html
    assert "data-question-text" in question_html and 'maxlength="4000"' in question_html
    assert "Submit all answers" in question_html and "모든 답변 제출" in question_html
    assert question_html.count("data-submit-question-set") == 1
    assert "leafWorkspaceQuestionSetHtml" in message and "questionsHtml" in message
    assert ".leaf-workspace-question-set" in html
    assert "[data-question-option]:checked" in bind
    assert "[data-question-text]" in bind
    assert "kind:'answer_questions'" in bind
    assert "answers.length+' answers submitted" in bind
    assert "summary.join('\\n')" in bind


def test_conversations_bundle_atkinson_hyperlegible_regular_and_bold():
    html = _html()

    font_dir = ROOT / "livingpc" / "ui" / "assets" / "fonts"
    assert (font_dir / "AtkinsonHyperlegible-Regular.ttf").is_file()
    assert (font_dir / "AtkinsonHyperlegible-Bold.ttf").is_file()
    assert (font_dir / "OFL.txt").is_file()
    assert html.count("@font-face") >= 2
    assert "AtkinsonHyperlegible-Regular.ttf" in html
    assert "AtkinsonHyperlegible-Bold.ttf" in html
    assert "function conversationHtml" in html
    assert "return '<strong>'+inner+'</strong>';" in html
    assert 'class="node-link"' in html


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


def test_leaf_workspace_clear_copy_warns_full_reset_in_both_languages():
    script = _script()
    clear = _function_body(script, "clearLeafWorkspaceConversation")

    assert "goal_leaf_workspace_clear" in clear
    assert "Reset this Leaf’s workspace?" in clear
    assert "Completion records, evidence, and approved handoffs remain" in clear
    assert "이 Leaf의 작업 공간을 초기화할까요?" in clear
    assert "완료 기록, 증거, 승인된 인계는 유지돼요" in clear


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
    assert "leaf-coach-message assistant leaf-workspace-thinking" in loading
    assert "messages.appendChild(thinking)" in loading
    assert "thinking.setAttribute('role','status')" in loading
    assert "scroller.scrollTop=scroller.scrollHeight" in loading
    assert ".leaf-workspace-thinking" in _html()
    assert "Faerie is crafting a response" in send and "페어리가 답변을 만드는 중" in send
    assert "optimistic.dataset.optimistic='true'" in send
    assert send.index("messageBox.appendChild(optimistic)") < send.index("leafWorkspaceSetLoading(true")
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
    assert "#faerie-mascot,#faerie-mascot * { animation:none !important; }" not in html


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
    assert "#faerie-mascot.dragging .mascot-giggle-drop" in html


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
    assert "'Show Faerie Mascot':'페어리 마스코트 표시'" in script
    assert "'Open Faerie chat':'페어리 채팅 열기'" in script


def test_dark_fae_mascot_skin_is_no_longer_user_selectable():
    html = _html()
    script = _script()
    refresh = _function_body(script, "refreshFaerieMascotSetting")
    setter = _function_body(script, "setFaerieMascotSkin")
    asset = ROOT / "livingpc" / "ui" / "assets" / "dark-fae-mascot.png"

    assert asset.exists() and asset.stat().st_size > 50_000
    assert asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert 'id="settings-mascot-skins"' in html
    assert '<button type="button" data-mascot-skin="classic">Pixel Faerie</button>' in html
    assert '<option value="dark">' not in html
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
    assert "['classic','cat','knight','meditate'].includes" in script
    assert "'Faerie Style':'페어리 스타일'" in script
    assert "Dark flame faerie" not in html


def test_pixel_cupid_cat_skin_is_transparent_selectable_and_animated():
    html = _html()
    script = _script()
    asset = ROOT / "livingpc" / "ui" / "assets" / "cupid-cat-mascot.png"

    assert asset.exists() and asset.stat().st_size > 50_000
    assert asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert '<button type="button" data-mascot-skin="cat">Cupid Flower Cat</button>' in html
    assert 'src="assets/cupid-cat-mascot.png"' in html
    assert "cupid-cat-aura" in html and "cupid-cat-heart" in html
    assert "@keyframes cupid-cat-breathe" in html
    assert "@keyframes cupid-cat-heart" in html
    assert "['classic','cat','knight','meditate'].includes" in script
    assert "'Cupid Flower Cat':'큐피드 꽃 고양이'" in script


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
    assert '<button type="button" data-mascot-skin="knight">Knight Star Cat</button>' in html
    assert '<button type="button" data-mascot-skin="meditate">Meditating Lotus Cat</button>' in html
    assert 'src="assets/knight-cat-mascot.png"' in html
    assert 'src="assets/meditating-cat-mascot.png"' in html
    assert "knight-cat-skin" in html and "meditating-cat-skin" in html
    assert "@keyframes pixel-cat-breathe" in html
    assert "@keyframes pixel-cat-particle" in html
    assert "'Knight Star Cat':'별의 기사 고양이'" in script
    assert "'Meditating Lotus Cat':'명상하는 연꽃 고양이'" in script


def test_sidebar_quote_and_duplicate_mascot_tooltip_are_removed():
    html = _html()
    assert "Stay curious, seeker." not in html
    assert 'aria-label="Open Faerie chat"' in html
    assert 'title="Open Faerie chat"' not in html
    assert 'data-help-en="Open Faerie Chat"' in html


def test_settings_are_grouped_explained_and_use_an_in_app_soul_name_editor():
    html = _html()
    script = _script()
    editor = _function_body(script, "openEditSoul")
    reports = _function_body(script, "runActivityReport")

    for heading in ("Soul & Account", "Appearance & Language", "Usage & Reports", "Advanced Tools"):
        assert heading in html
    assert "Edit Soul Name" in html
    assert 'id="soul-name-modal"' in html
    assert "soul-name-modal').classList.add('open')" in editor
    assert "prompt('Soul name:'" not in script
    assert "Soul purpose (why this Soul exists)" not in script
    assert 'id="settings-usage" class="settings-usage-card"' in html
    assert 'id="settings-report-kind"' in html
    assert "Today’s Report — Activity From Today" in html
    assert "Full Report — Complete Activity History" in html
    assert 'id="settings-generate-report"' in html
    assert "generate_daily_report" in reports and "generate_full_report" in reports
    assert "data-help-en=" in html and "data-help-ko=" in html


def test_settings_use_only_the_active_language_and_explain_today_cost():
    html = _html()
    script = _script()
    usage = _function_body(script, "refreshSettingsUsageLine")
    language = _function_body(script, "refreshSettingsLanguageLabel")

    settings = html[html.index('id="settings-drawer"'):html.index('id="soul-name-modal"')]
    assert "API Usage · 사용량" not in settings
    assert ">Language<span" in settings
    assert "Language · 언어" not in settings
    assert 'id="settings-usage-breakdown"' in settings
    assert "Main Chat" in usage and "GoalAI / Leaf" in usage and "Today Focus" in usage
    assert "+usd.toFixed(2)+' USD'" in usage
    assert "Current: English — change language" in language
    assert "현재: 한국어 — 언어 변경" in language


def test_settings_language_change_waits_for_confirmed_restart():
    html = _html()
    script = _script()
    open_modal = _function_body(script, "openLanguageRestartModal")
    confirm = _function_body(script, "confirmLanguageRestart")

    assert 'id="language-restart-modal"' in html
    assert "Nothing on the current screen changes before confirmation" in open_modal
    assert "Save any text you have not submitted yet" in open_modal
    assert "Investigation answer drafts are saved automatically" in open_modal
    assert "pendingLanguageTarget=(APP_LANG==='ko')?'en':'ko'" in open_modal
    assert "app_set_language" not in open_modal
    assert "enableKoreanUI" not in open_modal
    assert "app_restart_language(pendingLanguageTarget,currentView)" in confirm
    assert "$('settings-language').onclick=openLanguageRestartModal" in script


def test_command_composer_shows_slash_menu_and_typing_state_as_chat_feedback():
    html = _html()
    script = _script()
    menu = _function_body(script, "renderCommandMenu")
    bind = _function_body(script, "bindCommandCenter")

    assert 'id="cc-command-menu"' in html
    for command in ("/browser ", "/file ", "/undo ", "/projects", "/skills", "/teach ", "/recalibrate"):
        assert "value:'" + command + "'" in script
    assert "commandCenterCommands.filter" in menu
    assert "renderCommandMenu(input.value)" in bind
    assert "loadCommandCenterCommands()" in bind
    assert "command_commands" in script
    assert "Message Faerie… Type / for commands, or use + to attach context." in html
    log_pos = html.index('id="cc-log"')
    status_pos = html.index('id="cc-status"')
    compose_pos = html.index('<div class="cc-compose-input-wrap">')
    assert log_pos < status_pos < compose_pos
    assert "cc-chat-status" in html
    assert "cc-compose-status" not in html


def test_main_chat_accepts_bounded_multi_file_drag_and_drop_attachments():
    html = _html()
    script = _script()
    bind = _function_body(script, "bindCommandCenter")
    reader = _function_body(script, "commandDroppedFileData")

    assert 'id="cc-drop-overlay"' in html
    assert "Drop files to attach" in html
    assert ".cc-chat-main.file-drop-active .cc-drop-overlay" in html
    assert "COMMAND_DROP_MAX_FILES=8" in script
    assert "COMMAND_DROP_MAX_BYTES=20_000_000" in script
    assert "command_attach_dropped_file(name,file.type||'',data)" in script
    assert "commandAttachments.push(result.attachment)" in script
    assert "reader.readAsDataURL(file)" in reader
    assert "chatPanel.addEventListener('dragenter'" in bind
    assert "chatPanel.addEventListener('drop'" in bind
    assert "event.dataTransfer.dropEffect='copy'" in bind
    assert "event.preventDefault()" in bind


def test_command_center_renders_and_approves_distinct_proposal_cards():
    script = _script()
    render = _function_body(script, "renderPendingProposal")
    approve = _function_body(script, "approvePendingProposal")
    dismiss = _function_body(script, "dismissPendingProposal")

    assert "pending_proposals" in render
    assert "proposals.map" in render
    assert "data-cc-proposal-approve" in render
    assert "data-cc-proposal-dismiss" in render
    assert "command_approve_proposal(index)" in approve
    assert "command_dismiss_proposal(index)" in dismiss
    assert "Applying proposal" in approve
    gui = (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "def command_approve_proposal" in gui
    assert "def command_dismiss_proposal" in gui
    assert "def command_commands" in gui


def test_command_center_offers_per_chat_growth_and_investigation_proposal_modes():
    html = (ROOT / "livingpc/ui/memory.html").read_text(encoding="utf-8")
    gui = (ROOT / "gui.py").read_text(encoding="utf-8")

    assert 'id="cc-new-chat-menu"' in html
    assert "Growth + Investigation-aware" in html
    assert "Proposal-free" in html
    assert 'id="cc-proposal-mode"' in html
    assert "command_new_chat(enabled)" in html
    assert "command_set_chat_proposals_enabled(enabled)" in html
    assert "def command_new_chat(self, proposals_enabled=True)" in gui
    assert "def command_set_chat_proposals_enabled" in gui


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


def test_mascot_choices_preview_on_hover_and_commit_on_click():
    html = _html()
    script = _script()
    preview = _function_body(script, "previewFaerieMascotSkin")

    assert html.count('data-mascot-skin="') == 4
    assert "mascot.dataset.skin=skin" in preview
    assert "button.onpointerenter=()=>previewFaerieMascotSkin" in script
    assert "button.onclick=()=>setFaerieMascotSkin" in script
    assert "mascotSkinChoices.onpointerleave=()=>previewFaerieMascotSkin(faerieMascotSkin)" in script


def test_mascot_stays_visible_over_overlays_and_drawers():
    html = _html()

    assert "body:has(.planner-drawer.open) #faerie-mascot" not in html
    assert "body:has(.inquiry-drawer.open) #faerie-mascot" not in html
    assert "body:has(.onboard-overlay.open) #faerie-mascot" not in html
    assert "body:has(.growth-map-overlay.open) #faerie-mascot" not in html
    assert "#faerie-mascot { position:fixed; left:84px; bottom:61px; z-index:95" in html


def test_launch_onboarding_continues_directly_into_soul_calibration():
    script = _script()
    success = "if(!r||r.ok===false){ onboardError('onboard-soul-error'"
    start = script.index(success)
    continuation = script[start:start + 500]

    assert "closeOnboardingAndEnter();" in continuation
    assert "openSoulCalDrawer();" in continuation
    assert continuation.index("closeOnboardingAndEnter();") < continuation.index("openSoulCalDrawer();")


def test_skipping_api_key_still_requires_naming_the_soul():
    script = _script()
    start = script.index("const skipBtn=$('onboard-skip-btn')")
    end = script.index("const soulBtn=$('onboard-soul-continue')", start)
    skip_handler = script[start:end]
    soul_handler = script[end:end + 1300]

    assert "onboardShowStep('soul')" in skip_handler
    assert "onboarding_skip" not in skip_handler
    assert "if(restoreAuthMode)" in skip_handler
    assert skip_handler.index("closeOnboardingAndEnter") < skip_handler.index("onboardShowStep('soul')")
    restore_branch = skip_handler[skip_handler.index("if(restoreAuthMode)"):skip_handler.index("onboardShowStep('soul')")]
    assert "return;" in restore_branch
    assert "if(!title)" in soul_handler
    assert "Name your Soul before continuing." in soul_handler


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
    assert "Clarify & plan in chat" in script
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


def test_new_suggestion_plans_in_chat_before_final_placement_review():
    script = _script()
    placement = _function_body(script, "renderSuggestionPlacement")
    request = _function_body(script, "reviewSuggestionPlacement")
    chat_first = _function_body(script, "startSuggestionPlannerBeforePlacement")
    planner = _function_body(script, "openGoalPlanner")

    assert "goal_plan_placement" in request
    assert "goal_plan_placement" in chat_first and "openGoalPlanner" in chat_first
    assert "user_confirmed:false" in chat_first and "review_required:true" in chat_first
    assert "After you summarize the plan" in chat_first
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
    review = _function_body(script, "reviewSuggestionImplementation")
    assert "startSuggestionPlannerBeforePlacement" in review


def test_growth_restructure_flow_previews_preserved_data_before_approval():
    script = _script()
    panel = _function_body(script, "renderGoalRestructurePanel")
    manual = _function_body(script, "renderGoalRestructureManual")
    proposal = _function_body(script, "goalProposalSummaryHtml")
    detail = _function_body(script, "renderGoalDetail")
    focus = _function_body(script, "renderGoalFocusPanel")
    edit_actions = _function_body(script, "goalEditNodeActionsHtml")
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
    assert "goal-focus-ask-command" in focus
    assert "goalEditNodeActionsHtml(node" in focus
    assert "Edit this " in edit_actions and "Restructure" in edit_actions
    assert "renderGoalRestructurePanel(node,$('goal-focus-restructure-panel'))" in bind_focus
    assert "#goal-focus-panel .agent-proposal[data-pid]" in bind_focus


def test_growth_nodes_have_reversible_archive_controls_in_both_views_and_languages():
    script = _script()
    focus = _function_body(script, "renderGoalFocusPanel")
    detail = _function_body(script, "renderGoalDetail")
    lifecycle = _function_body(script, "goalLifecycleButtonHtml")
    edit_actions = _function_body(script, "goalEditNodeActionsHtml")
    binding = _function_body(script, "bindGoalArchiveControls")

    assert "goalEditNodeActionsHtml(node" in focus
    assert "goalLifecycleButtonHtml(node)" in edit_actions
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
    # The "area" role renders as "Branch" (Root -> Branch -> Project -> Leaf).
    assert "area:'Branch'" in semantic and "project:'Project'" in semantic and "stage:'Stage'" in semantic
    assert "area:'가지'" in semantic and "project:'프로젝트'" in semantic and "stage:'단계'" in semantic
    assert "goalTypeLabel(node.type,node)" in constellation
    assert 'class="legend-area">◇ Branch' in html
    assert 'class="legend-project">◇ Project' in html
    assert 'class="legend-stage">◇ Stage' in html
    assert "'◇ Branch':'◇ 가지'" in script
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


def test_growth_map_uses_order_based_focus_and_priority_dropdown():
    html = _html()
    script = _script()
    constellation = _function_body(script, "renderGoalConstellation")
    actions = _function_body(script, "goalProjectSignalActionsHtml")
    order_field = _function_body(script, "goalPriorityOrderFieldHtml")
    derive = _function_body(script, "goalDerivePlanningRoles")
    signal_label = _function_body(script, "goalProjectSignalLabel")

    assert "structurePrefix" in constellation and "goalTypeLabel(node.type,node)" in constellation
    # Legend reflects the order-based model.
    assert "NOW = the single focus Leaf · numbers = priority order" in html
    # The priority/current signal checkboxes are retired.
    assert "currently_working" not in actions and "highest_priority" not in actions
    assert 'type="checkbox"' not in actions
    # Priority is set by a per-node order dropdown that reorders via goal_move.
    assert 'id="goal-position"' in order_field and "Priority order" in order_field
    assert "<select" in order_field
    assert "goal_move(node.id,parseInt($('goal-parent').value,10)" in script
    # A single order-based FOCUS Project + its one NOW Leaf.
    assert "project_focus.focus=true" in derive
    assert "leaves[0].planning_role='now'" in derive
    assert "'FOCUS'" in signal_label


def test_constellation_badges_fit_their_rendered_text():
    script = _script()
    fitting = _function_body(script, "fitConstellationBadges")
    constellation = _function_body(script, "renderGoalConstellation")

    assert "text.getComputedTextLength()" in fitting
    assert "measured+16" in fitting
    assert "rect.setAttribute('width'" in fitting
    assert "fitConstellationBadges(box)" in constellation


def test_growth_horizon_roles_are_visible_and_position_controls_sequence():
    html = _html()
    script = _script()
    ordering = _function_body(script, "goalSortedActiveLeaves")
    active = _function_body(script, "goalActiveLeaves")
    next_action = _function_body(script, "goalNextActionText")
    label = _function_body(script, "goalPlanningRoleLabel")
    chip = _function_body(script, "goalPlanningRoleChip")
    derive = _function_body(script, "goalDerivePlanningRoles")
    focus = _function_body(script, "renderGoalFocusPanel")
    detail = _function_body(script, "renderGoalDetail")
    constellation = _function_body(script, "renderGoalConstellation")

    assert ordering.index("a.position") < ordering.index("a.id")
    assert "priority" not in ordering and "due_date" not in ordering
    assert "['active','paused']" in active
    assert "goalSortedActiveLeaves(node)" in next_action
    # One-Leaf model: NOW is the only planning role — no TENTATIVE NEXT.
    assert "'NOW'" in label and "TENTATIVE NEXT" not in label
    assert "node.planning_role" in label and "goal-planning-role" in chip
    # Order-based focus: a single focus Project derived from sibling order.
    assert "['active','paused']" in derive
    assert "leaves[0].planning_role='now'" in derive
    assert "leaves[1].planning_role" not in derive
    assert "focusProject" in derive and "collect(focusProject)" in derive
    assert "currentProject" not in derive and "priorityArea" not in derive
    assert "goalPlanningRoleChip(node)" in focus
    assert "goalPlanningRoleChip(leaf)" in focus
    assert "goalPlanningRoleChip(node)" in detail
    assert "planning-role-badge" in constellation
    assert "goalPlanningRoleLabel(node)" in constellation
    assert ".goal-planning-role.now" in html


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
    repair = _function_body(script, "repairLeafWorkspaceHandoff")
    advance = _function_body(script, "refreshAfterLeafCompletion")

    assert 'id="leaf-workspace-incoming"' in html
    for field in ("output_summary", "working_material", "constraints",
                  "unresolved_questions", "suggested_start"):
        assert f'data-proposal-handoff-field="{field}"' in editor
    assert "payload.handoff" in edited and "proposalHandoffField" in edited
    assert "Approved handoff from an earlier Leaf" in incoming
    assert "이전 Leaf에서 승인된 인계" in incoming
    assert "This older handoff contains only a summary" in incoming
    assert "Restore missing artifact" in incoming
    assert "data-repair-handoff" in incoming
    assert "goal_leaf_workspace_repair_handoff" in repair
    assert "Restoring the original artifact" in repair
    assert "repairLeafWorkspaceHandoff(button)" in render
    assert "raw conversation" in render and "원문 대화" in render
    assert "handoffLeafId?goalFind" in advance
    assert "view.completion_handoff||view.recovery_handoff" in script


def test_completed_legacy_leaf_can_prepare_an_editable_approved_only_handoff():
    html = _html()
    script = _script()
    recovery = _function_body(script, "leafWorkspaceRecoveryHtml")
    prepare = _function_body(script, "prepareMissingLeafHandoff")
    editor = _function_body(script, "leafWorkspaceProposalEditorHtml")
    decision = _function_body(script, "decideLeafWorkspaceProposal")

    assert 'id="leaf-workspace-recovery"' in html
    assert "Prepare Missing Handoff" in recovery and "누락된 인계 준비" in recovery
    assert "raw conversation" in recovery and "원문 대화" in recovery
    assert "goal_leaf_workspace_prepare_handoff" in prepare
    assert "handoff_recovery" in editor
    assert "Approve handoff" in script and "인계 승인" in script
    assert "proposal.type==='handoff_recovery'" in decision
    assert "view.recovery_handoff" in decision


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
    assert "Continue to chat" in render
    assert "이 질문들은 모두 선택 사항이에요." in render
    assert "대신 챗봇과 대화하기" in render
    assert "soul-cal-chat" in bind and "activateView('self')" in bind
    assert "setSoulCalibrationEnabled(false)" in bind
    assert "Go to Settings to try it again anytime." in bind


def test_soul_calibration_is_settings_only_and_has_a_durable_checkbox():
    html = _html()
    script = _script()
    hydrate = _function_body(script, "hydrateDurableUiPreferences")

    assert 'id="self-dashboard"' not in html
    assert "function selfSidebarHtml" not in script
    assert 'id="settings-calibration-enabled" checked' in html
    assert "ffSoulCalibrationEnabled" in script
    assert "persistDurableUiPreference('soul_calibration_enabled'" in script
    assert "preferences,'soul_calibration_enabled'" in hydrate
    assert "if(e.target.checked)" in script and "openSoulCalDrawer();" in script


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


def test_command_center_hides_persisted_attachment_context_from_message_bubbles():
    script = _script()
    message_text = _function_body(script, "commandMessageText")

    assert "ATTACHED_DOCUMENT_CONTEXT" in message_text
    assert "text.slice(0,markerIndex)" in message_text


def test_approved_chat_context_is_visible_in_the_investigation_record():
    script = _script()
    context = _function_body(script, "curApprovedContextHtml")
    card = _function_body(script, "curCardHtml")

    assert "cur.investigation_contexts" in context
    assert "Approved conversation context" in context
    assert "future Investigation questions and syntheses" in context
    assert "curApprovedContextHtml(cur)" in card


def test_investigations_first_open_has_loading_retry_and_single_inflight_request():
    script = _script()
    activate = _function_body(script, "activateView")
    loading = _function_body(script, "showCuriosityLoading")
    failure = _function_body(script, "showCuriosityLoadError")
    load = _function_body(script, "loadCuriosity")

    assert "Loading investigations" in loading
    assert "탐구 불러오는 중" in loading
    assert "Investigations did not finish loading" in failure
    assert "cur-load-retry" in failure
    assert "loadCuriosity({force:true})" in failure
    assert "curiosityLoadPromise&&!options.force" in load
    assert "loadBackgroundImages().catch" in load
    assert "requestAnimationFrame" in load
    assert "catch(error){ reject(error); }" in load
    assert "curiosityLoadSequence" in load
    assert "showCuriosityLoadError" in load
    assert "Promise.resolve(investigationLoad).then" in activate


def test_investigation_shelf_search_grows_and_keeps_keyboard_focus():
    html = _html()
    script = _script()
    shelf = _function_body(script, "curShelfHtml")
    card = _function_body(script, "curShelfCardHtml")
    bind = _function_body(script, "bindCuriosityOverview")

    # Cards render grouped into completion-tier sections (no flat map, no tabs).
    assert "group.items.map(c=>curShelfCardHtml(c,selected))" in shelf
    assert "cur-shelf-tier" in shelf and "curCompletionTier" in shelf
    assert "cur-board-filters" not in shelf  # filter tabs removed from the shelf
    assert "CUR_SHELF_PAGE_SIZE" not in script
    assert "cur-shelf-prev" not in shelf and "cur-shelf-next" not in shelf
    assert "curBoardQuery=search.value" in bind
    assert "const cursor=search.selectionStart" in bind
    assert "refreshed.focus()" in bind
    assert "refreshed.setSelectionRange(cursor,cursor)" in bind
    assert 'tabindex="0" role="button"' in card
    assert "card.onclick=event=>" in bind
    assert "event.key==='Enter'||event.key===' '" in bind
    assert "overflow-y:auto" in html
    assert ".cur-shelf-card:hover" in html


def test_investigation_advanced_record_is_a_drawer_and_primary_learning_tools_stay_visible():
    html = _html()
    script = _script()
    overview = _function_body(script, "curOverviewHtml")
    open_details = _function_body(script, "openCuriosityDetails")
    close_details = _function_body(script, "closeCuriosityDetails")
    related = _function_body(script, "bindRelatedInvestigationPanel")
    thread_controls = _function_body(script, "bindCuriosityThreadControls")

    assert '<aside id="cur-management"' in html
    assert '<details id="cur-management"' not in html
    assert "cur-management-backdrop" in html
    assert "data-cur-explore" in overview
    assert "data-cur-explore-help" in overview
    assert "data-cur-explore-example" in overview
    assert "Work situations" in overview
    assert "data-cur-synthesize" in overview
    assert "details.hidden=false" in open_details
    assert "details.hidden=true" in close_details
    assert "card.querySelector('.cur-thread-add')" not in related
    assert "card.querySelector('.cur-thread-add')" in thread_controls


def test_exploration_thread_picker_uses_current_investigation_context_and_three_choices():
    script = _script()
    overview = _function_body(script, "curOverviewHtml")
    suggest = _function_body(script, "addCuriosityThread")
    choices = _function_body(script, "curExplorationSuggestionsHtml")
    bind = _function_body(script, "bindCuriosityOverview")

    assert "data-cur-explore" in overview
    assert "curiosity_thread_suggest(cur.id)" in suggest
    assert "Finding three directions" in suggest
    assert "Suggested exploration directions" in choices
    assert "data-cur-thread-option" in choices
    assert "createCuriosityThread" in bind


def test_proposal_checkpoint_keeps_questions_available_and_labels_relevance():
    script = _script()
    overview = _function_body(script, "curOverviewHtml")
    bind = _function_body(script, "bindCuriosityOverview")
    proposal = _function_body(script, "curSuggestionHtml")

    assert "data-cur-answer-more" in overview
    assert "Answer more questions" in overview
    assert "beginCuriositySession(selected,btn)" in bind
    assert "still_relevant" in proposal
    assert "needs_revision" in proposal
    assert "possibly_stale" in proposal
    assert "relevance_revised_text" in proposal


def test_long_working_interpretations_are_split_at_readable_sentence_boundaries():
    script = _script()
    paragraphs = _function_body(script, "curParagraphsHtml")
    review = _function_body(script, "curUnderstandingReviewHtml")

    assert "clean.length<520" in paragraphs
    assert "const target=clean.length/2" in paragraphs
    assert "sentences.slice(0,split)" in paragraphs
    assert "curParagraphsHtml(p.interpretation" in review
    assert "curParagraphsHtml((previous.payload||{}).interpretation" in review


def test_investigation_loading_buttons_keep_visible_labels_and_restore_after_failure():
    html = _html()
    script = _script()
    overview = _function_body(script, "bindCuriosityOverview")
    session = _function_body(script, "renderCuriositySession")
    generate = _function_body(script, "continueCuriosity")

    assert "button > .chat-thinking" in html
    assert "color:inherit" in html
    assert "button:disabled:has(> .chat-thinking)" in html
    assert "const originalButtonHtml=create.innerHTML" in overview
    assert "create.innerHTML=originalButtonHtml" in overview
    assert "const originalButtonHtml=button.innerHTML" in session
    assert "button.innerHTML=originalButtonHtml" in session
    assert "const originalButtonHtml=button?button.innerHTML:''" in generate


def test_investigation_card_offers_direct_add_note_input():
    """The Investigation card must let the user steer it without a chat
    round-trip: a note panel wired to the curiosity_add_note bridge."""
    script = _script()
    card = _function_body(script, "curCardHtml")
    note = _function_body(script, "curAddNoteHtml")
    bind = _function_body(script, "bindCurCard")

    assert "curAddNoteHtml(cur)" in card
    assert "cur-note-text" in note
    assert "cur-note-save" in note
    assert "pywebview.api.curiosity_add_note(cur.id,note)" in bind
    # An empty note must never reach the backend.
    assert "Write a note first." in bind


def test_chat_logs_support_ctrl_wheel_zoom_with_persisted_scale():
    script = _script()
    assert "bindChatZoom('cc-log')" in script
    assert "bindChatZoom('leaf-coach-messages')" in script
    zoom = _function_body(script, "bindChatZoom")
    assert "if(!e.ctrlKey) return;" in zoom
    assert "e.preventDefault();" in zoom
    assert "{passive:false}" in zoom
    assert "localStorage.setItem(key,String(scale))" in zoom
    # Bounded so a runaway wheel can't make the chat unreadable.
    assert "Math.min(2,Math.max(.6," in zoom


def test_leaf_completion_triggers_background_debrief_in_main_chat():
    """Approving a completion must automatically ask the main-chat companion
    to analyze the path (DEBRIEF MOMENT), without yanking the user out of
    Growth: background command_send plus a clickable notice."""
    script = _script()
    debrief = _function_body(script, "debriefLeafCompletionInChat")
    refresh = _function_body(script, "refreshAfterLeafCompletion")

    assert "pywebview.api.command_send(message,[])" in debrief
    assert "[Leaf completed]" in debrief
    assert "switchView('self')" in debrief
    # Each debrief opens its own chat so it never buries a conversation.
    assert "command_new_chat(true)" in debrief
    # The debrief explicitly invites NEW Investigations for surfaced tensions
    # and new Branch/Project structure when learnings outgrow the project.
    assert "propose a NEW Investigation" in debrief
    assert "create_branch" in debrief
    assert "if(!recoveredHandoff) debriefLeafCompletionInChat(leafId);" in refresh


def test_clear_leaf_conversation_is_a_full_reset_that_restarts_automatically():
    script = _script()
    clear = _function_body(script, "clearLeafWorkspaceConversation")
    assert "all related working data is reset" in clear
    assert "This cannot be undone." in clear
    assert "pywebview.api.goal_leaf_workspace_open(leafId)" in clear


def test_chat_markdown_renders_nested_bold_italics_and_headings():
    script = _script()
    body = _function_body(script, "conversationHtml")
    # Bold must tolerate single asterisks inside it (nested italics).
    assert r"\*\*((?:[^*\n]|\*(?!\*))+)\*\*" in body
    # Single-asterisk italics and #-headings render instead of showing raw.
    assert "<em>$2</em>" in body
    assert "chat-heading" in body


def test_main_chat_renders_clickable_reply_choices_scoped_to_their_chat():
    script = _script()
    render = _function_body(script, "renderCommandChat")
    send = _function_body(script, "sendCommandMessage")

    assert "cc-reply-choice" in render
    assert "ccReplyChoicesChat" in render               # scoped to the right chat
    assert "sendCommandMessage();" in render            # click sends the answer
    assert "ccReplyChoices=(r.reply_choices)||[];" in send
    assert "ccReplyChoices=[]; ccReplyChoicesChat=null;" in send


def test_every_selection_button_has_a_copy_to_input_edit_affordance():
    """Each clickable answer (main-chat pills, Leaf suggestions, multi-select
    items, question options) carries a pencil button that copies its text into
    the input box for editing instead of sending it as-is."""
    script = _script()
    render = _function_body(script, "renderCommandChat")
    coach = _function_body(script, "leafCoachMessageHtml")
    questions = _function_body(script, "leafWorkspaceQuestionSetHtml")
    bind = _function_body(script, "leafWorkspaceBindActions")

    assert "data-cc-choice-edit" in render
    assert "input.focus()" in render                      # edit copies, never sends
    assert coach.count("leafSuggestionEditButtonHtml()") == 2   # single + multi
    assert "leafSuggestionEditButtonHtml()" in questions        # choice options
    assert "[data-edit-suggestion]" in bind
    assert "input.value=value+' '" in bind


def test_switching_tabs_never_stomps_an_in_flight_chat_send():
    """Reloading Command Center state while a reply is mid-flight used to wipe
    the thinking status and optimistic message, making the send look
    cancelled. The reload now defers to the in-flight send."""
    script = _script()
    load = _function_body(script, "loadCommandChat")
    send = _function_body(script, "sendCommandMessage")

    assert "if(ccSendInFlight()) return;" in load
    assert "ccSendInFlightSince=Date.now();" in send
    assert send.count("ccSendInFlightSince=0;") == 2       # success + failure
    # The guard self-expires so a hung reply can't block reloads forever.
    guard = _function_body(script, "ccSendInFlight")
    assert "180000" in guard
