from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
    Spacer, PageBreak, Table, TableStyle, KeepTogether)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon, Circle
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "pdf" / "faerie-fire-upward-spiral-implementation.pdf"
W, H = letter
INK = colors.HexColor("#211B2D")
MUTED = colors.HexColor("#655E70")
PURPLE = colors.HexColor("#6F4AB5")
LAV = colors.HexColor("#EEE7FA")
MINT = colors.HexColor("#DFF3E8")
GOLD = colors.HexColor("#F4D58D")
ROSE = colors.HexColor("#F8E2E7")
PAPER = colors.HexColor("#FCFAF6")
WHITE = colors.white


styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleFF", parent=styles["Title"], fontName="Helvetica-Bold",
    fontSize=28, leading=32, textColor=INK, alignment=TA_LEFT, spaceAfter=14))
styles.add(ParagraphStyle(name="SubFF", parent=styles["Normal"], fontName="Helvetica",
    fontSize=13, leading=18, textColor=MUTED, spaceAfter=12))
styles.add(ParagraphStyle(name="H1FF", parent=styles["Heading1"], fontName="Helvetica-Bold",
    fontSize=21, leading=25, textColor=INK, spaceAfter=12))
styles.add(ParagraphStyle(name="H2FF", parent=styles["Heading2"], fontName="Helvetica-Bold",
    fontSize=12, leading=15, textColor=PURPLE, spaceBefore=7, spaceAfter=5))
styles.add(ParagraphStyle(name="BodyFF", parent=styles["BodyText"], fontName="Helvetica",
    fontSize=9.4, leading=13.2, textColor=INK, spaceAfter=7))
styles.add(ParagraphStyle(name="SmallFF", parent=styles["BodyText"], fontName="Helvetica",
    fontSize=7.6, leading=10, textColor=INK))
styles.add(ParagraphStyle(name="SmallWhite", parent=styles["BodyText"], fontName="Helvetica-Bold",
    fontSize=7.8, leading=9.5, textColor=WHITE, alignment=TA_CENTER))
styles.add(ParagraphStyle(name="Callout", parent=styles["BodyText"], fontName="Helvetica-Bold",
    fontSize=11, leading=15, textColor=INK, backColor=LAV, borderColor=PURPLE,
    borderWidth=1, borderPadding=10, spaceBefore=8, spaceAfter=10))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, W, H, fill=1, stroke=0)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(0.62 * inch, 0.35 * inch, "FAERIE FIRE - UPWARD SPIRAL IMPLEMENTATION")
    canvas.drawRightString(W - 0.62 * inch, 0.35 * inch, f"{doc.page}")
    canvas.setStrokeColor(LAV)
    canvas.line(0.62 * inch, 0.52 * inch, W - 0.62 * inch, 0.52 * inch)
    canvas.restoreState()


def arrow(d, x1, y1, x2, y2, color=PURPLE):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=1.8))
    import math
    a = math.atan2(y2-y1, x2-x1)
    size = 6
    pts = [x2, y2,
           x2-size*math.cos(a-.55), y2-size*math.sin(a-.55),
           x2-size*math.cos(a+.55), y2-size*math.sin(a+.55)]
    d.add(Polygon(pts, fillColor=color, strokeColor=color))


def node(d, x, y, w, h, title, subtitle="", fill=LAV, stroke=PURPLE):
    d.add(Rect(x, y, w, h, rx=8, ry=8, fillColor=fill, strokeColor=stroke, strokeWidth=1.2))
    d.add(String(x+w/2, y+h-16, title, textAnchor="middle", fontName="Helvetica-Bold",
                 fontSize=8.5, fillColor=INK))
    if subtitle:
        lines = subtitle.split("|")
        for i, line in enumerate(lines[:3]):
            d.add(String(x+w/2, y+h-29-i*10, line, textAnchor="middle",
                         fontName="Helvetica", fontSize=6.8, fillColor=MUTED))


def spiral_diagram():
    d = Drawing(500, 245)
    coords = [(30,145),(155,180),(300,168),(390,85),(250,30),(95,45)]
    names = [("Evidence","answers, memories, outcomes"),("Investigation","versioned interpretation"),
             ("Experiment","chosen small Leaf"),("Outcome","what actually happened"),
             ("Revision","confidence can fall"),("Better next step","or archive the goal")]
    for i, ((x,y),(title,sub)) in enumerate(zip(coords,names)):
        node(d,x,y,105,48,title,sub, MINT if i in {0,3} else LAV)
        nx,ny=coords[(i+1)%len(coords)]
        arrow(d,x+52,y+(0 if i in {2,3} else 48),nx+52,ny+48 if i in {3,4} else ny,
              PURPLE)
    d.add(Circle(250,115,30,fillColor=GOLD,strokeColor=colors.HexColor("#B78A27")))
    d.add(String(250,119,"UPWARD",textAnchor="middle",fontName="Helvetica-Bold",fontSize=9,fillColor=INK))
    d.add(String(250,106,"SPIRAL",textAnchor="middle",fontName="Helvetica-Bold",fontSize=9,fillColor=INK))
    return d


def lifecycle_diagram():
    d=Drawing(500,260)
    xs=[15,135,255,375]
    node(d,xs[0],185,100,50,"Candidate","start | refine | defer")
    node(d,xs[1],185,100,50,"Gathering","answers + evidence",MINT)
    node(d,xs[2],185,100,50,"Draft","uncertain synthesis",GOLD)
    node(d,xs[3],185,100,50,"Approved","user decision",MINT)
    for i in range(3): arrow(d,xs[i]+100,210,xs[i+1],210)
    node(d,255,75,100,55,"Reopened","contradiction | outcome|new evidence",ROSE)
    arrow(d,425,185,325,130)
    arrow(d,305,75,305,185)
    node(d,15,75,100,55,"Blocked","reject | never ask",ROSE)
    arrow(d,65,185,65,130)
    node(d,375,75,100,55,"Archived","purpose served",colors.HexColor("#E8E6EA"),MUTED)
    arrow(d,425,185,425,130)
    return d


def tree_diagram():
    d=Drawing(500,280)
    node(d,190,220,120,45,"Soul","the life being actualized",GOLD,colors.HexColor("#B78A27"))
    node(d,65,145,120,45,"Root","chosen direction",LAV)
    node(d,315,145,120,45,"Root","another direction",LAV)
    node(d,65,70,120,45,"Branch","strategy or capability",MINT)
    node(d,315,70,120,45,"Historical","paused or archived",colors.HexColor("#E8E6EA"),MUTED)
    node(d,5,5,110,40,"Leaf","small experiment",ROSE)
    node(d,135,5,110,40,"Next Leaf","approved adjustment",ROSE)
    arrow(d,250,220,125,190); arrow(d,250,220,375,190)
    arrow(d,125,145,125,115); arrow(d,125,70,60,45); arrow(d,125,70,190,45)
    arrow(d,375,145,375,115)
    return d


def cadence_diagram():
    d=Drawing(500,260)
    labels=[("Trigger","new evidence or quiet goal"),("Gate","dedupe + backlog under 3"),
            ("Wait","quiet hours or weekly cap"),("Prompt","highest priority only")]
    xs=[10,135,260,385]
    for i,(t,s) in enumerate(labels):
        node(d,xs[i],175,105,55,t,s, MINT if i in {0,3} else LAV)
        if i<3: arrow(d,xs[i]+105,202,xs[i+1],202)
    outcomes=[("Helpful","record useful"),("Snooze","3, 6, 12, 24 days"),
              ("Too much","30, 60, 90 days"),("Never","stop this topic")]
    for i,(t,s) in enumerate(outcomes):
        x=10+i*125
        node(d,x,45,105,55,t,s, GOLD if i==0 else ROSE)
        arrow(d,437,175,x+52,100,MUTED)
    return d


def table(data, widths, header=True, small=False):
    cooked=[]
    style=styles["SmallFF"] if small else styles["BodyFF"]
    for row in data:
        cooked.append([Paragraph(str(cell), style) for cell in row])
    t=Table(cooked,colWidths=widths,repeatRows=1 if header else 0,hAlign="LEFT")
    commands=[("VALIGN",(0,0),(-1,-1),"TOP"),("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#D7D0DF")),
              ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
              ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]
    if header:
        commands += [("BACKGROUND",(0,0),(-1,0),PURPLE),("TEXTCOLOR",(0,0),(-1,0),WHITE)]
        for cell in cooked[0]: cell.style = styles["SmallWhite"]
    for r in range(1 if header else 0,len(cooked)):
        if r%2==0: commands.append(("BACKGROUND",(0,r),(-1,r),colors.HexColor("#F5F1F8")))
    t.setStyle(TableStyle(commands)); return t


story=[]
story += [Spacer(1,0.45*inch), Paragraph("Faerie Fire",styles["SubFF"]),
          Paragraph("The Upward-Spiral System",styles["TitleFF"]),
          Paragraph("What was implemented, how the person-model changes over time, and how consent keeps reflection useful.",styles["SubFF"]),
          Spacer(1,0.15*inch), spiral_diagram(),
          Paragraph("The central promise",styles["H2FF"]),
          Paragraph("Faerie does not try to finish describing a person. It keeps a versioned, correctable interpretation that can become narrower, less confident, or obsolete as life changes.",styles["Callout"]),
          Paragraph("Implementation report - Phase 1 through Phase 6 | Verified focused suite: 266 passing tests | July 12, 2026",styles["SmallFF"]), PageBreak()]

story += [Paragraph("1. System architecture and authority",styles["H1FF"]),
          Paragraph("Each layer has one job. Facts, interpretations, chosen directions, and notification timing are deliberately separate so the model cannot turn a guess into identity or change a life plan without permission.",styles["BodyFF"]),
          table([["Layer","Allowed role","User authority","History"],
                 ["Memory","What happened or was explicitly said","Existing memory controls","Superseded facts remain"],
                 ["Person-model claim","Tentative pattern across evidence","Confirm, reject, or correct","Old wording and evidence remain"],
                 ["Investigation synthesis","Best current answer to one question","Approve, correct, reject, reopen","Every version is preserved"],
                 ["Suggested Investigation","Possibly useful uncertainty","Start, refine, defer, reject, never ask","Decisions block repetition"],
                 ["Growth tree","Chosen direction and experiments","Owns every mutation","Archived nodes stay visible"],
                 ["GoalAI proposal","Possible tree adjustment","Separate approval required","Stale proposals cannot apply"],
                 ["Reflection cadence","When Faerie may seek attention","Act, snooze, ignore, stop topic","Metadata only"]],
                [1.05*inch,2.0*inch,1.55*inch,1.6*inch],small=True),
          Spacer(1,8), Paragraph("Core safety boundary",styles["H2FF"]),
          Paragraph("Model output is a proposal. Approval of an Investigation synthesis does not silently approve a person-model change or a Growth-tree mutation; those are separate decisions.",styles["Callout"]), PageBreak()]

story += [Paragraph("2. How Investigations change over time",styles["H1FF"]), lifecycle_diagram(),
          Paragraph("A candidate never starts itself",styles["H2FF"]),
          Paragraph("Faerie ranks possible questions by relevance, uncertainty, expected usefulness, burden, and sensitivity. At most two are visible and active capacity is capped at five. Sensitive topics require explicit permission.",styles["BodyFF"]),
          Paragraph("A synthesis is a working interpretation",styles["H2FF"]),
          Paragraph("Each version stores confidence, evidence, counterevidence, unknowns, possible experiments, what changed, and reopen conditions. Meaningful answers or experiment outcomes make a new draft due. The user approves before it can influence downstream understanding.",styles["BodyFF"]),
          table([["New evidence","Expected response"],["Supporting answer","Confidence may rise, with source attached"],
                 ["Contradictory answer","Confidence may fall; interpretation may narrow"],
                 ["Successful exception","Create a new version; preserve the earlier generalization"],
                 ["Unhelpful experiment","Draft a lower-confidence synthesis"],
                 ["Insufficient evidence","Say so; keep unknowns explicit"]],[2.1*inch,4.1*inch]), PageBreak()]

story += [Paragraph("3. How the Growth tree grows - and prunes",styles["H1FF"]),tree_diagram(),
          Paragraph("Growth is not permanent accumulation",styles["H2FF"]),
          Paragraph("Roots are chosen life directions, Branches are strategies or capabilities, and Leaves are small experiments. Outcomes flow back as evidence. Reusable learning can be harvested upward, but a proposed change still requires approval.",styles["BodyFF"]),
          table([["Gardening proposal","Purpose"],["Rewrite","Update language to match the person's current meaning"],
                 ["Split","Separate a broad or conflicted direction"],["Merge","Combine overlapping directions"],
                 ["Pause","Keep it without active pressure"],["Archive","Move it into visible history"],
                 ["Attach evidence","Ground the node without changing its meaning"],["Leave unchanged","Record that the goal still fits"]],[2.0*inch,4.2*inch]),
          Paragraph("New evidence can trigger a relevance review. A quiet active goal gets a gentle check after 30 days; descendant activity resets the clock. Paused and archived goals stay quiet.",styles["Callout"]), PageBreak()]

story += [Paragraph("4. Outcomes close the learning loop",styles["H1FF"]),
          Paragraph("A Leaf is an experiment, not a moral test. Completing, attempting, avoiding, and abandoning can all reveal something useful.",styles["SubFF"]),
          table([["Outcome field","Why it matters"],["Result","Completed, attempted, avoided, or abandoned"],
                 ["What happened","Factual evidence, separate from interpretation"],
                 ["Expected obstacle","Compares the prediction to real life"],
                 ["Surprise","Finds exceptions and missing variables"],
                 ["Helpfulness (1-5)","Bad advice must reduce confidence"],
                 ["Changed understanding","User-authored correction to the working model"],
                 ["Next adjustment","Creates an approval-gated next Leaf proposal"]],[2.0*inch,4.2*inch]),
          Spacer(1,10), Paragraph("Propagation after an outcome",styles["H2FF"]),
          table([["Destination","What is added","What is not allowed"],
                 ["Goal evidence","Structured outcome reference","Automatic goal rewrite"],
                 ["Memory","Encrypted factual outcome","Identity-level conclusion"],
                 ["Inference","Evidence on a relevant claim","Silent claim confirmation"],
                 ["Investigation","Synthesis readiness and counterevidence","Overwriting old synthesis"],
                 ["Growth tree","Proposed next Leaf","Automatic child creation"]],[1.4*inch,2.35*inch,2.45*inch]),
          Paragraph("This is the upward turn: lived reality is allowed to disprove Faerie.",styles["Callout"]), PageBreak()]

story += [Paragraph("5. Reflection rhythm and consent",styles["H1FF"]), cadence_diagram(),
          Paragraph("One shared gate",styles["H2FF"]),
          Paragraph("Investigation check-ins, inference reviews, newly graduated hypotheses, and GoalAI updates all use the same local cadence. They cannot each send their own weekly prompt. Contradictions rank above routine check-ins but never bypass the cap.",styles["BodyFF"]),
          table([["Default","Behavior"],["Global limit","At most one unsolicited reflection every 7 days"],
                 ["Quiet hours","21:00 through 08:00 local time"],["Backlog","At most 3 queued reflection subjects"],
                 ["Snooze","3, 6, 12, then 24 days; capped at 28"],["Ignore","30, 60, then 90 days of topic-level quiet"],
                 ["Never","Durably disables that prompt topic"],["Explicit /remind","Bypasses cadence because the user scheduled it"]],[1.55*inch,4.65*inch]),
          Paragraph("The cadence database stores no prompt body and no private answer - only kind, opaque subject key, trigger, priority, timestamps, state, and optional usefulness/burden ratings.",styles["Callout"]), PageBreak()]

story += [Paragraph("6. Phases delivered",styles["H1FF"]),
          table([["Phase","Implemented capability","Primary safeguard"],
                 ["1","Versioned Investigation synthesis","Approval before downstream influence"],
                 ["2","Person-model reconciliation: support, contradict, narrow, retire, situational, change over time","Strong identity claims need stronger evidence"],
                 ["3","Suggested Investigation engine","No auto-start; sensitive permission; durable rejection"],
                 ["4","Tree relevance and gardening","Every mutation is approval-gated"],
                 ["5","Structured experiment outcomes","Bad advice lowers confidence; history remains"],
                 ["6","Shared cadence and longitudinal evaluation","Quiet hours, weekly cap, backlog, suppression"]],[0.55*inch,3.45*inch,2.2*inch],small=True),
          Spacer(1,10), Paragraph("Synthetic multi-month journeys",styles["H2FF"]),
          table([["Journey","Contract verified"],["Changed dream","Can be reconsidered without erasing old meaning"],
                 ["Mistaken fear hypothesis","Contradiction gets priority; weekly consent remains"],
                 ["Successful exception","New synthesis version narrows the old generalization"],
                 ["Sensitive rejection","Never-ask remains durable across future attempts"],
                 ["Repeated burden","Snooze and ignore intervals expand"],["Conflicting evidence","History and ability to change are preserved"]],[2.0*inch,4.2*inch]),
          Paragraph("Focused verification: 266 passing tests across inference, Investigations, GoalAI, goals, outcomes, scheduler behavior, UI bridges, cadence policy, and longitudinal journeys.",styles["Callout"]), PageBreak()]

story += [Paragraph("7. What remains to learn from real use",styles["H1FF"]),
          Paragraph("The architecture is complete enough to begin longitudinal product tuning. The remaining questions are human, not merely technical.",styles["SubFF"]),
          table([["Question","What to watch"],["Is weekly too quiet or too frequent?","Prompt usefulness and burden ratings over several months"],
                 ["Are goal checks timely?","Whether 30 quiet days feels supportive or guilt-inducing"],
                 ["Are suggestions insightful?","Start/refine/defer/reject/never-ask ratios"],
                 ["Does Faerie change its mind fairly?","Whether users recognize the cited evidence and explanation"],
                 ["Do outcomes feel lightweight?","Completion rate and whether the form feels like homework"],
                 ["Does the tree remain legible?","Growth versus archive rate and usefulness of history"]],[2.25*inch,3.95*inch]),
          Paragraph("Known engineering constraints",styles["H2FF"]),
          Paragraph("The Growth UI still shares the large memory.html file, so modularization would reduce the blast radius of future interface changes. The cadence uses explicit subject keys rather than broad semantic similarity; Suggested Investigations retain their stronger topic-key rejection boundary. Real burden data should guide tuning rather than optimizing for engagement.",styles["BodyFF"]),
          Paragraph("Recommended checkpoint",styles["H2FF"]),
          Paragraph("Use the system normally for several weeks. Then review: which prompt arrived, why it arrived, whether it was useful, whether it felt heavy, and what Faerie changed afterward. That is the feedback loop that should tune the next version.",styles["Callout"]),
          Spacer(1,18), Paragraph("Faerie should feel attentive, corrigible, and increasingly specific - never certain about a person merely because it has accumulated data.",styles["SubFF"])]


doc=BaseDocTemplate(str(OUT),pagesize=letter,leftMargin=0.62*inch,rightMargin=0.62*inch,
                    topMargin=0.62*inch,bottomMargin=0.68*inch,title="Faerie Fire Upward-Spiral Implementation",
                    author="Faerie Fire")
frame=Frame(doc.leftMargin,doc.bottomMargin,doc.width,doc.height,id="main")
doc.addPageTemplates([PageTemplate(id="paper",frames=[frame],onPage=footer)])
doc.build(story)
print(OUT)
