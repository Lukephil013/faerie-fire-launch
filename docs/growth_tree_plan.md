# Growth Tree Plan

Replace the Growth tab's row-list with a zoomable, procedurally drawn glowing
tree. Approving a proposal jumps to Growth, shows the new part growing on the
whole tree, then zooms into it. Decided: procedural SVG tree (no painted
image), Roots rendered as main limbs (underground roots are decorative), and
the tree fully replaces the old list view.

## Concept

The tree IS the data. `GoalStore.tree()` already returns
Soul → Roots → Branches → Leaves with progress, mastery, and status. Render
that hierarchy as an organic SVG: Soul as the glowing trunk core, each Root as
a major limb radiating from it, Branches as sub-limbs, Leaves as leaf clusters
at the tips. Decorative underground roots and ambient background sell the
mood without carrying data.

Less information on screen: no toolbar stats grid, no permanent detail pane.
Just the tree, a zoom control, and a collapsible "Needs Attention" sidebar.
Detail appears only after zooming into a node.

## Layout engine (deterministic, stable)

- Pure function `layoutTree(root) -> {nodes:[{id,x,y,angle,depth,path}], edges}`.
- Roots get angular slots around the trunk ordered by `position`; children fan
  within the parent's angular wedge. Slight organic jitter seeded by node id
  (hash) so positions never shuffle between renders — a node keeps its spot
  for life, and new siblings insert without relayout of existing limbs.
- Edge geometry: cubic Béziers with seeded curvature; limb stroke width tapers
  with depth. Leaves render as small glowing foliage blobs.
- Everything lives in one SVG with a large fixed coordinate space
  (e.g. 4000×3000); zoom/pan is purely `viewBox` manipulation.

## Interaction

- Pan (drag), wheel/pinch zoom, and the −/100%/+ / fit controls from the
  mockup, all as tweened `viewBox` transitions (rAF, ~450ms ease).
- Click a node → tween viewBox to frame that node's subtree → slide in a
  compact detail card (right side): title, type chip, description, next
  action, progress/mastery, and buttons that open the existing edit drawer,
  planner drawer, and GoalAI chat (all reused as-is).
- "Back to tree" zooms out to full frame; Esc does the same.
- Node labels fade in by zoom level: at full view only Root labels + node
  counts show; Branch/Leaf labels appear as you zoom in.

## Growth animation + approval handoff

- Sequence `growNode(id)`: switch to Growth tab → render tree with the new
  edge at `stroke-dashoffset` hidden → 1) brief hold on full tree, 2) draw-in
  of the new limb (dashoffset animate, ~1.2s) with a traveling glow pulse and
  a few particle sparks, 3) tween-zoom into the new node, 4) detail card
  slides in with a "New" badge.
- Trigger: everywhere a proposal approval creates a goal node (Investigations
  classification accepts, GoalAI `goalProposalAction(...,'approve')`, planner
  `commit_plan`), the response already/easily includes created node ids.
  Set `pendingGrowth = [ids]`, call `activateView('goals')`; `loadGoals()`
  consumes it. Multiple ids queue sequentially.
- Manual creation (New Root, plan commit) uses the same path — one animation
  system, no special cases.
- Fallback: `prefers-reduced-motion` or a config flag skips to step 4.

## Needs Attention sidebar

Left panel, collapsible, max ~5 cards + "View all". Aggregated client-side
from existing endpoints (no new store queries needed initially):

- Pending GoalAI proposals (approve/adjust) — high priority.
- Open inference/belief reviews touching a linked goal.
- Curiosity questions awaiting answers on linked investigations.
- Stale leaves (no evidence in N days) — later, needs a small query.

Clicking a card zooms to the related node and opens its card, or deep-links
to the owning tab (e.g. Investigations) when the item lives there.

## Phases

1. **Layout + render**: `layoutTree`, SVG render from `tree()`, pan/zoom with
   controls. Old list still default; new view behind the "Open full map"
   button for development.
2. **Zoom-to-node + detail card**: click targets, viewBox tweens, card wired
   to existing edit/planner/chat drawers.
3. **Sidebar**: attention aggregation + zoom-to-node linking.
4. **Growth animation + handoff**: `pendingGrowth` queue, draw-in animation,
   wiring from the three approval sites.
5. **Swap + cleanup**: tree becomes `view-goals`; remove row-list, stats
   grid, and growth-map overlay; ambient background, mini-map (small inset
   with current viewport rectangle), polish.

## Constraints

- All in `livingpc/ui/memory.html` (existing single-file pattern); no new
  Python endpoints for phases 1–4 except returning created node ids from
  approval calls if any site doesn't already.
- pywebview WebView2: SVG + CSS transitions are safe; avoid heavy filters on
  every frame (pre-render glow with `feGaussianBlur` on static layers, animate
  only the active limb).
- Invariants untouched: proposals still require explicit approval; the tree
  only visualizes committed state. Rejections never appear.
- Tests: layout determinism (same tree → same coordinates) can be tested by
  extracting `layoutTree` into a small testable JS block or mirroring the
  seed-hash logic in Python; bridge behavior stays covered by
  `tests/test_ui_bridges.py`.
