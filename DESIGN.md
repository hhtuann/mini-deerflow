<role>
You are an expert frontend engineer, UI/UX designer, visual design specialist, and typography expert. Your goal is to help the user integrate a design system into an existing codebase in a way that is visually consistent, maintainable, and idiomatic to their tech stack.

Before proposing or writing any code, first build a clear mental model of the current system:
- Identify the tech stack (e.g. React, Next.js, Vue, Tailwind, shadcn/ui, etc.).
- Understand the existing design tokens (colors, spacing, typography, radii, shadows), global styles, and utility patterns.
- Review the current component architecture (atoms/molecules/organisms, layout primitives, etc.) and naming conventions.
- Note any constraints (legacy CSS, design library in use, performance or bundle-size considerations).

Ask the user focused questions to understand the user's goals. Do they want:
- a specific component or page redesigned in the new style,
- existing components refactored to the new system, or
- new pages/features built entirely in the new style?

Once you understand the context and scope, do the following:
- Propose a concise implementation plan that follows best practices, prioritizing:
  - centralizing design tokens,
  - reusability and composability of components,
  - minimizing duplication and one-off styles,
  - long-term maintainability and clear naming.
- When writing code, match the user’s existing patterns (folder structure, naming, styling approach, and component patterns).
- Explain your reasoning briefly as you go, so the user understands *why* you’re making certain architectural or design choices.

Always aim to:
- Preserve or improve accessibility.
- Maintain visual consistency with the provided design system.
- Leave the codebase in a cleaner, more coherent state than you found it.
- Ensure layouts are responsive and usable across devices.
- Make deliberate, creative design choices (layout, motion, interaction details, and typography) that express the design system’s personality instead of producing a generic or boilerplate UI.

</role>

<design-system>
# Design Style: Corporate Trust

## 1. Design Philosophy
This style embodies the **modern enterprise SaaS aesthetic** — professional yet approachable, sophisticated yet friendly. It draws inspiration from tech unicorns and high-growth startups that have successfully humanized the corporate experience. The design rejects the cold, sterile formality of traditional corporate websites in favor of a warm, confident, and inviting presence.

**Core Principles:**
- **Trustworthy Yet Vibrant**: Establishes credibility through clean structure and professional typography while maintaining visual energy through vibrant gradients and colorful accents
- **Dimensional Depth**: Uses isometric perspectives, soft colored shadows, and subtle 3D transforms to create visual interest and break free from flat design
- **Refined Elegance**: Every element is polished with attention to micro-interactions, smooth transitions, and sophisticated hover states
- **Purposeful Gradients**: Indigo-to-violet gradients serve as the visual signature, used strategically in headlines, buttons, and decorative elements
- **Professional Polish**: Generous white space, consistent spacing rhythms, and crisp typography create a premium, enterprise-ready feel

**Keywords**: Trustworthy, Vibrant, Polished, Dimensional, Modern, Approachable, Enterprise-Ready, Elegant

**Visual DNA**: The unmistakable signature of this style comes from:
1. **Colored Shadows**: Soft shadows with blue/purple tints instead of neutral grays
2. **Isometric Elements**: Subtle 3D transforms (rotate-x, rotate-y) on decorative cards and visualizations
3. **Gradient Text**: Strategic use of gradient text for emphasis in headlines
4. **Soft Blobs**: Large, blurred gradient orbs in the background for atmospheric depth
5. **Elevated Cards**: White cards that lift on hover with enhanced shadows
6. **Dual-Tone Palette**: Indigo (primary) + Violet (secondary) creating a cohesive gradient spectrum

## 2. Design Token System

### Colors (Light Mode)
*   **Background**: `#F8FAFC` (Slate 50) - A very subtle cool grey/white base.
*   **Foreground (Surface)**: `#FFFFFF` (White) - For cards and raised elements.
*   **Primary**: `#4F46E5` (Indigo 600) - The core brand color. Vibrant blue-purple.
*   **Secondary**: `#7C3AED` (Violet 600) - For gradients and accents.
*   **Text Main**: `#0F172A` (Slate 900) - High contrast, sharp.
*   **Text Muted**: `#64748B` (Slate 500) - For supporting text.
*   **Accent/Success**: `#10B981` (Emerald 500) - For positive indicators.
*   **Border**: `#E2E8F0` (Slate 200) - Subtle separation.

### Typography
*   **Font Family**: `Plus Jakarta Sans` — A geometric sans-serif with friendly rounded terminals that perfectly balances professional authority with modern approachability. Its clean letterforms ensure excellent readability while maintaining visual warmth.
*   **Scaling**: Major Third (1.250) scale provides substantial hierarchy without overwhelming the layout
*   **Font Weights**:
    *   **Display/Headings**: ExtraBold (800) for hero headlines, Bold (700) for section headings
    *   **Subheadings**: SemiBold (600) for card titles and emphasis
    *   **Body Text**: Regular (400) for paragraphs, Medium (500) for navigation and labels
*   **Line Heights**:
    *   Headlines: 1.1 (tight tracking for impact)
    *   Body Text: 1.6-1.7 (relaxed for readability)
*   **Letter Spacing**: Tight tracking (-0.02em) on large headlines for modern polish
*   **Responsive Type Scale**:
    *   Mobile: text-2xl to text-4xl for h1
    *   Desktop: text-4xl to text-6xl for h1
    *   Progressive scaling ensures legibility across all devices

### Radius & Border
*   **Radius**: `rounded-xl` (12px) for cards and `rounded-lg` (8px) for inputs. Buttons are `rounded-full` or `rounded-lg`.
*   **Borders**: Thin, 1px borders using the `Border` token.

### Shadows & Effects
This is where the design truly shines. **Colored shadows** replace neutral grays to reinforce the brand palette:

*   **Default Card Shadow**: `0 4px 20px -2px rgba(79, 70, 229, 0.1)` — Soft blue-tinted base elevation
*   **Hover Card Shadow**: `0 10px 25px -5px rgba(79, 70, 229, 0.15), 0 8px 10px -6px rgba(79, 70, 229, 0.1)` — Multi-layer depth on interaction
*   **Button Shadow**: `0 4px 14px 0 rgba(79, 70, 229, 0.3)` — Strong presence for primary CTAs
*   **Glow Effects**: Numbered badges use `shadow-[0_0_20px_rgba(79,70,229,0.5)]` for ethereal glow
*   **Background Blobs**: Large gradient orbs with 3xl blur create atmospheric depth without distraction
    *   `blur-3xl filter` combined with low opacity (20-50%)
    *   Positioned absolutely to create layered depth
*   **Gradients**:
    *   **Primary Gradient**: `from-indigo-600 to-violet-600` — Used for buttons and active states
    *   **Text Gradient**: Combined with `bg-clip-text text-transparent` for striking headlines
    *   **Background Gradients**: Subtle `from-indigo-100 to-violet-100` for container backgrounds
    *   **Final CTA Background**: `from-indigo-900 to-indigo-950` for dramatic dark section

## 3. Component Stylings

### Buttons
*   **Primary**: Gradient background (Indigo to Violet). `rounded-full` or `rounded-lg`. White text. Slight shadow. Transition: Lift (`-translate-y-0.5`) and increase shadow on hover.
*   **Secondary**: White background, Border `E2E8F0`, Text `Slate 700`. Hover: `bg-slate-50` and darker border.

### Cards
*   **Base**: White background, `rounded-xl`, `border border-slate-100`, `shadow-soft`.
*   **Behavior**: On hover, slight lift and increased shadow intensity.
*   **Feature Cards**: May feature an icon in a soft-colored circle (bg-indigo-50 text-indigo-600).

### Inputs
*   **Style**: `bg-white`, `border-slate-200`, `rounded-lg`.
*   **Focus**: `ring-2 ring-indigo-500 ring-offset-1` and `border-indigo-500`.
*   **Label**: `text-sm font-semibold text-slate-700`.

## 4. Non-Generic Bold Choices

The Corporate Trust aesthetic stands out through deliberate, sophisticated design decisions:

### Isometric Depth & 3D Transforms
*   **Hero Card**: `perspective-[2000px]` parent with `rotate-x-[5deg] rotate-y-[-12deg]` child creates subtle isometric effect
*   **Hover Transforms**: `hover:rotate-x-[2deg] hover:rotate-y-[-8deg]` — Subtle 3D movement on interaction
*   **Feature Cards**: Alternating `rotate-y-[6deg]` and `rotate-y-[-6deg]` based on layout position
*   **Benefit Visualization**: `rotate-x-6 rotate-y-12 transform` on gradient container for dramatic depth

### Strategic Gradient Usage
*   **Split Headlines**: First 3 words in standard color, remaining words in gradient for visual hierarchy
*   **Gradient Buttons**: Full background gradient with hover lift (`-translate-y-0.5`)
*   **Badge Elements**: NEW badge with solid indigo background inside gradient-ringed container
*   **Final CTA**: White button on dark gradient background creates dramatic contrast

### Atmospheric Background Elements
*   **Blur Orbs**: Large (400-600px) circular gradients with heavy blur positioned absolutely
*   **Layered Positioning**: Multiple blobs at different z-indexes create depth
*   **Subtle Animation**: `animate-pulse duration-[4000ms]` on floating cards for gentle movement

### Elevated Card System
*   **Default State**: Soft colored shadow with subtle border
*   **Hover State**: Lift effect (`-translate-y-1`) combined with enhanced shadow
*   **Transition**: Smooth `duration-200` for professional polish
*   **Pricing Highlight**: Center card uses `md:scale-105` with special ring styling

### Micro-Interactions
*   **Arrow Icons**: `transition-transform group-hover:translate-x-1` for directional feedback
*   **Image Zoom**: `group-hover:scale-105` on blog images with overlay fade-in
*   **Chevron Rotation**: `group-open:rotate-180` for FAQ accordions
*   **Button Lift**: Subtle upward movement on hover reinforces clickability

## 5. Spacing & Layout
*   **Container**: `max-w-7xl` (1280px) provides spacious, enterprise-appropriate width
*   **Padding**: Responsive padding with `px-4 sm:px-6` pattern for consistent gutters
*   **Vertical Rhythm**:
    *   Mobile: `py-16` (64px)
    *   Tablet: `sm:py-20` (80px)
    *   Desktop: `lg:py-24` (96px)
*   **Section Spacing**: Generous white space between sections creates breathing room
*   **Grid Strategy**:
    *   Hero: Two-column `lg:grid-cols-2` with text-first approach
    *   Features: Alternating zig-zag with `lg:flex-row` and `lg:flex-row-reverse`
    *   Pricing: Three-column `md:grid-cols-3` with center emphasis
    *   Stats: Four-column `md:grid-cols-4` for metric display
*   **Responsive Breakpoints**:
    *   Mobile-first approach with progressive enhancement
    *   sm: 640px, md: 768px, lg: 1024px, xl: 1280px
*   **Text Width Constraints**: `max-w-xl` or `max-w-2xl` on paragraphs to maintain 60-75 character line lengths

## 6. Animation & Transitions
*   **Philosophy**: "Refined Motion" — Smooth, professional, never jarring
*   **Base Transition**: `transition-all duration-200` for general interactive elements
*   **Long Transitions**: `duration-500` for image zooms and complex animations
*   **Hover Effects**:
    *   Cards: Combine `hover:-translate-y-1` with shadow enhancement
    *   Buttons: `hover:-translate-y-0.5` for subtle lift
    *   Icons: `transition-transform group-hover:translate-x-1` for directional cues
*   **Easing**: Default `ease-out` for natural deceleration
*   **Pulse Animation**: `animate-pulse duration-[4000ms]` on decorative floating elements for gentle breathing effect
*   **State Changes**: Smooth color transitions on links and buttons reinforce interactivity

## 7. Iconography
*   **Library**: `lucide-react` for consistent, modern icon system
*   **Style**:
    *   Default stroke width: `2px` (standard)
    *   Size: `h-4 w-4` for inline icons, `h-5 w-5` or `h-6 w-6` for featured icons
    *   Joins: Rounded for friendliness
*   **Color Treatment**:
    *   **Badge Icons**: Icon in `text-indigo-600` on `bg-indigo-100` container
    *   **Navigation Icons**: Inherit text color, transition on hover
    *   **Social Icons**: `text-slate-400 hover:text-indigo-400`
*   **Icon Containers**:
    *   Small badges: `h-12 w-12 rounded-xl` with soft background
    *   Large features: `h-14 w-14 rounded-xl` for prominent sections
    *   Circular: `rounded-full` for avatars or status indicators
*   **Accessibility**: Icons are decorative with proper text alternatives or hidden from screen readers when paired with text

## 8. Responsive Strategy
*   **Mobile-First Philosophy**: Design begins at 375px width, progressively enhances
*   **Touch Targets**: Minimum 44x44px for all interactive elements (buttons, links)
*   **Typography Scaling**:
    *   Headlines reduce from `text-6xl` (desktop) to `text-4xl` (mobile)
    *   Body text maintains readability at `text-base` with responsive line heights
*   **Layout Adaptations**:
    *   Two-column layouts stack to single column on mobile
    *   Navigation collapses to essential items (login hidden on mobile)
    *   Pricing cards stack vertically with equal width
    *   Footer columns stack progressively (4 col → 2 col → 1 col)
*   **Spacing Compression**: Padding and margins reduce proportionally on smaller screens
*   **Image Optimization**: Aspect ratios maintained, sizes adapt to container width
*   **Horizontal Scrolling**: Never required; all content fits viewport width
*   **Visual Hierarchy Preserved**: Even on mobile, clear distinction between heading levels maintained

## 9. Accessibility & Best Practices
*   **Color Contrast**: All text meets WCAG AA standards
    *   Slate 900 on Slate 50 background: AAA compliant
    *   White text on Indigo 900 background: AAA compliant
    *   Link colors tested for 4.5:1 minimum ratio
*   **Focus States**:
    *   Visible ring on all interactive elements: `focus-visible:ring-2 focus-visible:ring-indigo-500`
    *   Ring offset for clarity: `focus-visible:ring-offset-2`
    *   Never remove focus indicators
*   **Semantic HTML**:
    *   Proper heading hierarchy (h1 → h2 → h3)
    *   Native `<button>` elements for interactive actions
    *   `<nav>` for navigation, `<footer>` for footer
    *   Details/summary for FAQ accordions
*   **Image Alt Text**: Descriptive alternatives for all images
*   **Interactive States**:
    *   Hover: Visual feedback on all clickable elements
    *   Active: Subtle state change on click
    *   Disabled: Reduced opacity with `pointer-events-none`
*   **Motion Preferences**: Consider `prefers-reduced-motion` for users sensitive to animation
*   **Screen Reader Support**: Proper ARIA labels where semantic HTML insufficient
</design-system>

---

# Mini DeerFlow — Product Application UX Specification

**Status:** Normative addendum for Task 2.0 (Streamlit → React migration).

**Scope:** Phase 0/1 research-agent interface; future-friendly composition for Phase 2, **without** implementing presentation generation in Task 2.0.

**Primary experience:** A focused research workspace, not a SaaS landing page.

## 10. How to apply Corporate Trust to the application

The preceding Corporate Trust design system defines the brand's visual language. This product addendum defines how to apply it to Mini DeerFlow's functional interface.

**Precedence when implementing Task 2.0:**
1. Runtime/security contracts and verified product behavior in the existing repository.
2. Product interaction, information architecture and states defined in this addendum.
3. Corporate Trust color, typography, component and motion rules above.
4. Implementation details selected to fit the project's React/FastAPI architecture.

Do not recreate the generic marketing examples (hero, pricing, blog, FAQ, sales CTA or marketing footer). They illustrate styling only. Do not introduce unused screens or mock features. The existing Streamlit experience is the reference for *functionality*, not a pixel-perfect visual target. Preserve features through the new design.

The migration goal and scope are already settled. Do not interrupt autonomous implementation with broad preference questionnaires; document reasonable choices and ask only when a genuinely blocking specification conflict cannot be resolved from the source and this document.

### Product design principles

- **Conversation first:** the user request, response and follow-up composer are the primary surface.
- **Explainable execution:** the run graph, evidence and trace inspector provide meaningful detail on demand without overwhelming the chat.
- **Evidence before decoration:** source validity, tool failures, budget outcomes and skill identity must remain clear.
- **Scannable density:** use compact enterprise application typography for logs/tables; reserve display typography for empty states and onboarding.
- **Professional restraint:** use gradients on the primary action or identity accent; never put animated blobs, isometric tilts or heavy elevation behind dense trace/graph content.
- **Responsive by design:** full capability remains accessible at small widths even when the layout changes into drawers and tabs.

## 11. Application information architecture

Only the following areas are required for Task 2.0. Their content must come from the real backend, not static fixtures except for clearly labeled offline walkthrough behavior.

| Area | User goal | Required content / actions |
| --- | --- | --- |
| Application navigation | Find and resume work | New chat; saved conversations; selected conversation; live/offline mode identity |
| Chat workspace | Research interactively | User/assistant turns; bounded composer; submit follow-up; turn statuses; validated citations; resume interrupted run |
| Execution inspector | Understand agent behavior | Plan, workflow/run graph, tools/delegation, reviews/replans, evidence, trace, context metrics, existing research artifact |
| Skill selection | Apply optional turn guidance | Discover metadata; choose one skill; description/applicability; per-turn reset; historical skill ID |
| Job feedback | See what is happening | Actual server job status and safe progress; errors; interrupted recovery; no fabricated events |

Do not create a functional PPTX screen in Task 2.0; extend this application shell for a Presentation workspace only after Phase 2's backend capabilities exist. Do not show misleading fake presentation downloads or empty promises.

### Suggested desktop shell (not a pixel-perfect wireframe)

```text
┌──────────────────┬────────────────────────────────────┬─────────────────────────┐
│ Navigation       │ Chat workspace                     │ Execution inspector     │
│ Mini DeerFlow    │ Conversation header / run status  │ Graph / Plan / Evidence │
│ + New chat       │                                    │ Trace / Skills / Budget │
│ Conversations    │ User/assistant conversation        │ / Artifacts             │
│ Active thread    │                                    │                         │
│ Mode: live/off   │                                    │ Select node → details   │
│ Skill: next turn │ Composer / Send / Resume           │                         │
└──────────────────┴────────────────────────────────────┴─────────────────────────┘
```

Use a persistent top-level application identity and clear current-conversation context. The inspector may be collapsible or a drawer. The central chat always has layout priority; avoid three cramped columns on mid-sized screens.

### Responsive behavior

| Viewport | Expected treatment |
| --- | --- |
| Wide desktop (≥1280px) | Collapsible navigation ~240–280px; chat grows; optional right inspector ~340–440px |
| Small desktop/tablet landscape (1024–1279px) | Chat + sidebar or chat alone; inspector becomes an overlay/drawer |
| Tablet portrait (768–1023px) | Compact navigation; chat main; inspector as drawer/tab |
| Phone (375–767px) | Single content column; navigation drawer; inspector full-screen sheet or page; composer usable above keyboard |

Widths are starting points, not rigid layout constraints. Use `min-width: 0`, overflow containment and appropriate wrapping to prevent page-level horizontal scrolling. Code/trace lines may scroll horizontally **inside** their dedicated content viewport. Preserve 44×44px minimum touch targets.

## 12. Core application components and behaviors

### 12.1 Navigation and conversations

- Show the Mini DeerFlow identity, `New chat`, a scannable list of persisted conversations and the selected conversation state.
- Distinguish **Live** and **Offline walkthrough** prominently but neutrally. Offline is deterministic test evidence, not external research or real-model quality. Keep switching modes from accidentally reusing the wrong job/storage identity.
- Conversation rows need a useful short title, active/selected styling, keyboard focus and sensible empty/loading/error states. Do not invent timestamps or server states if unavailable.
- Reopening a conversation must restore turns, safe run details and artifacts supported by persistence. State is server-owned; browser state is not the source of truth.

### 12.2 Chat workspace

- User messages and assistant answers have distinct but restrained treatments; long answers remain easy to read.
- Assistant markdown must use safe rendering, bounded media and validated links/citations. Treat raw model output as untrusted text; do not inject arbitrary HTML.
- Use a clearly labeled composer with send and disabled/running states; `Enter` sends and `Shift+Enter` inserts a newline when consistent with the chosen accessible input implementation.
- Keep the composer conveniently accessible; on phones, account for software keyboard and safe-area insets.
- Show actual turn/job states: `pending`, `running`, `completed`, `interrupted`, `failed` as supported by backend. Do not imply that client animations prove tool activity.
- Provide an explicit resume action when the persisted turn is interruptible. Avoid double submission and misleading retries.
- Skill selected for the next turn is visible before send; after submission it resets unless the user explicitly selects it again.

### 12.3 Skills

- Skill chooser lists **metadata only** (ID, description, applicability), includes `None` and supports at most one selection for the next turn.
- Do not fetch or display skill instruction bodies as UI metadata, and do not imply a skill gives permissions or tools.
- Display historical skill ID on each completed/replayed turn or its details when present.
- Distinguish `selected`, `not selected`, `not available` and `instructions omitted by context budget` when the backend reports those states.

### 12.4 Read-only Agent Graph

- Offer explicit **Workflow** (possible topology) and **Current run** (observed execution) views. Label them so static edges are not confused with actually executed edges.
- Use real projected nodes/edges and stable IDs from the Python backend. Do not hardcode the offline alpha/beta fixture as the graph data model.
- Represent parent workflow steps, delegation, children, tool calls, fan-in and continuation if present in the projection. Do not invent causal edges.
- Required node statuses: `pending`, `running`, `completed`, `failed`, `cancelled`, `skipped` when reported; ensure non-color differentiation through labels/icons/shapes/legend.
- In partial failure, the failed child must remain clearly **failed** while Parent may finish successfully. Do not collapse every result into a green success badge.
- Click/tap a node to open a safe inspector (label, status, duration/time, counts, budget/outcome, safe explanation, relevant IDs). Offer accessible keyboard selection or an equivalent list/tree for users unable to operate the canvas.
- Support zoom, pan, fit-to-view and a layout stable enough for inspection; actual workflow/run data takes precedence over decorative 3D effects. On mobile, graph and inspector must not require horizontal page scrolling.
- Never make the graph editable in Task 2.0, and never expose raw prompts, internal chain of thought, credentials, unrestricted tool arguments or provider payloads.

### 12.5 Execution details

Use navigable tabs, sub-tabs or a collapsible inspector rather than stacking a long wall of tables. Display only actual fields made available by the backend's safe projection.

| Section | Minimum readable content |
| --- | --- |
| Plan | Ordered steps, active step, step status and high-level limits |
| Delegation / tools | Parent–child structure, branches, tool outcomes, partial failures, budgets and fan-in summary |
| Evidence | Title, canonical source URL, provenance, bounded excerpt, cited/not-cited flag |
| Review / replan | Verdict, reason, limitations and replacement-plan summary |
| Trace | Filterable event list, outcome, stage, safe time/ID values; readable loading/empty states |
| Context | Payload chars, budget chars, approximate token heuristic, retained/omitted/truncated items, skill omission and policy identity if reported |
| Artifact | Safe research report preview/source and exact existing download capability |

Evidence, raw observations and citations are not interchangeable. Avoid presenting a URL as fact-checked merely because the runtime accepts its source membership. Clearly identify heuristic token counts as **approximate**.

## 13. Visual rules for functional application screens

### Tokens and hierarchy

- Centralize the tokens defined above as CSS variables or Tailwind theme tokens. Reuse them consistently; do not invent a disconnected second palette.
- Light mode is **required** and specified. Dark mode is **not required** by this document; implement it only if a coherent full token set and tested contrast are supplied, not by simply inverting surfaces.
- Typography: Plus Jakarta Sans for navigation/content; use a sensible fallback stack without requiring online font requests. Use tabular numbers or a readable monospace style for IDs, metrics and logs.
- Typical density: navigation labels 13–14px, body 14–16px, section headings 16–20px; preserve generous line height in reading surfaces. The marketing display type scale is not appropriate for every application heading.
- Gradients belong on primary CTA/active identity accents and occasional empty-state accents. Flat surfaces, stable contrast and subtle borders are preferred inside inspectors.
- Use the provided radius, muted borders and colored shadow values. Reserve elevated/hover-lift cards for actionable summaries; do not animate every conversation row or graph node.

### Semantic status colors

Corporate Trust defines a success green but does not fully specify execution semantics. Add **semantic** error/warning/info/status tokens with verified WCAG AA foreground/background contrast. Do not reuse the brand violet to mean `failed` or green to mean `running` merely for appearance. Label every status in text and/or icon, never by color alone.

### Motion, accessibility and safety

- Keep short transitions around 150–250ms where useful; honor `prefers-reduced-motion` and disable nonessential motion.
- No animated backgrounds behind lengthy chat, evidence, graph inspector or traces. Avoid isometric tilt on interactive graph nodes, tables and code panels.
- Ensure visible focus, semantic headings, proper labels, ARIA live regions for **meaningful** job updates and focus management for dialogs/drawers.
- Handle loading, empty, timeout, disconnection, failed run, restored interrupted run and malformed/unavailable skill catalog without broken layout.
- Keep error messages safe and actionable; never surface private server tracebacks or credentials to the UI.

## 14. Data ownership and interaction truthfulness

- Python backend remains authoritative for conversation/turn status, graph projection, trace events, skill identities, evidence membership, artifact references and tool/budget enforcement.
- React owns only ephemeral presentation state: open tabs/drawers, selected inspector node, local composer draft and requested display filters.
- UI polling may update job state but must not manufacture event transitions. Persisted conversation reload/resume is mandatory.
- Use safe typed HTTP response DTOs; do not send unrestricted LangGraph `AgentState`, `SkillContext.instructions`, local absolute paths or model/provider secrets to the browser.
- A downloadable artifact must be served by an allowlisted server endpoint for the selected conversation/turn, not by a generic path parameter.
- Preserve existing offline demo truth labels and explicit live-mode distinctions.

## 15. Browser acceptance screens and evidence

For Task 2.0, capture real-browser screenshots when tooling is available (not synthetic mockups), at least for the **actual** offline walkthrough and the following states:

1. **Desktop chat**, 1440×900: sidebar + active conversation; inspector can open without crushing the composer.
2. **Desktop Current run graph**, 1440×900: inspect Parent, successful child, failed child, fan-in and continuation from actual offline trace data.
3. **Skills / Context**, desktop: selected skill metadata, turn-specific history and sanitized context-budget diagnostics.
4. **Phone**, 375×812: functional chat composer, navigation and inspector access; no page-wide horizontal overflow.
5. **Reload / resume / artifact**: persisted conversation loads, interrupted-state recovery if supported, and existing research report download is real.

Check keyboard access, text contrast, graph fallback accessibility, reduced motion and failure states. Differentiate `automated test PASS`, `browser inspected`, and `manual mentor acceptance pending`. If browser automation is unavailable, state that limitation explicitly; do not claim visual validation from unit tests alone.

## 16. Completion guardrails

Task 2.0 reaches **React feature parity** only when the new frontend reproduces the functional capabilities currently exposed by Streamlit through the actual Python runtime, including live/offline execution, chat/history/follow-up/resume, skills, graph, traces, evidence, context metrics and research artifact access.

The old Streamlit UI remains the reference/fallback until these behaviors are verified. Retire the Streamlit-only rendering files in a subsequent cleanup after acceptance; do **not** delete reusable demo services, fixtures, safe projections, graph logic or Phase 0/1 tests merely because they live under `demo/`.

**Definition of done:** design-system compliance + functional parity + backend/frontend regression + real offline end-to-end browser demonstration (or explicitly documented browser-verification gap). No PPTX implementation is required in Task 2.0.
