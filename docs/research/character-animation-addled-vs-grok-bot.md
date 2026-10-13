# Character animation: Addled's procedural blob vs Grok Bot

Brainstorm, 2026-10-13. Both sides read from source, not from watching.

- **Addled**: `backend/character/` (4,084 lines, PyQt6 `QPainter`, 30 fps).
- **Grok Bot**: read from a public reverse-engineering —
  `github.com/Dkrillex/grok-icon` (`docs/architecture.md`, `src/bot/*.ts`),
  plus the official statement by the animator that it is "entirely made in
  code, with every state, shape and expression able to morph smoothly".

---

## The one thing that differs

Both draw a body in code and morph it over time. **Addled morphs only its
*scale*. Grok morphs its *silhouette*.**

That single difference is where all the life comes from, and it is not a
polish gap — it is a data-model gap.

| | Addled | Grok Bot |
|---|---|---|
| Body primitive | A closed-form superellipse, `n = 2.2` | An array of **64 radii**, one per angle |
| Per frame | `painter.scale(k, k)` — **same k both axes** | `radii[i]` interpolated to a target array |
| New shape | a new `draw_*` function | a new **64-number array** |
| In-between shapes | impossible | free — that is what morphing *is* |
| Body reacts to state | no | yes, the state *is* a silhouette |

Addled's own code comments say the shape was chosen *because it cannot go
wrong*:

> A superellipse is used here rather than hand-placed Bézier control points
> because it cannot go wrong in the way those did.

That is a fair call for a static shape. But it also fixes the silhouette:
`rx`, `ry` and `n` are literals inside `draw_blob`, and the only per-frame
input is the painter transform. A closed form has no knobs a state can turn.

---

## What Grok actually does (verified in source)

### 1. A shape is a radial profile, sampled at the same 64 angles

`profiles.ts` / `shape.ts`:

```ts
const ANGLES = Array.from({ length: PROFILE_SAMPLES }, (_, i) => ...)  // 64
export function blend(a: Silhouette, b: Silhouette, t: number) {
  for (let i = 0; i < PROFILE_SAMPLES; i++)
    dst.radii[i] = lerp(a.radii[i], b.radii[i], t)   // linear, per angle
  ...
}
```

The architecture doc is explicit about why:

> All profiles are sampled at the same angles (64), so any two shapes have
> points that correspond one to one and a transition reduces to a linear
> interpolation of radii. **That is why there is no path-morphing library here.**

So a "shape" is `number[64]`. Morphing is `lerp` on an array. There is no
intermediate shape to author — every in-between is generated.

### 2. States *are* silhouettes

Not decoration on top of a body. From `states.ts`, actual state definitions:

```ts
{ id: 'triangle', pose: () => base({ sil: silhouette('triangle') }) }
{ id: 'egg',      pose: () => base({ sil: silhouette('egg') }) }
{ id: 'hexagon',  pose: () => base({ sil: silhouette('hexagon') }) }
```

and one that morphs between two bodies:

```ts
radii: tri.radii.map((r, i) => r + (ball.radii[i] - r) * back)
```

The character *becomes* the shape. That is where "every state can morph"
comes from.

### 3. Squash comes from the profile, not from `scale(sx, sy)`

A surprise: across the state catalogue, `sx`/`sy` are **1**. The squash is
baked into the radii. Only the idle breath touches an axis, and it is tiny
and one-sided:

```ts
sy: pose.sil.sy * life.breath     // Y only
breath = 1 + sin(t / 3.4 * TAU) * 0.005    // ±0.5%
```

Compare Addled: `painter.scale(pulse_scale, pulse_scale)` with
`pulse_scale` swinging ±3–5% on *both* axes. Grok breathes less, on one
axis; Addled inflates more, uniformly — which is the "balloon" tell.

### 4. The eyes are holes in a `<mask>`

> The eyes are holes in a `<mask>` — not white shapes laid on top. That is
> what makes them clip themselves against the silhouette when they slide
> towards the edge, with no cropping code.

Addled draws eyes as filled shapes (`_draw_eyes`, `drawEllipse`). They can
leave the body and nothing stops them.

### 5. Eyes follow the *real* radius at their angle

> The eyes live on a sphere of radius 1; on a non-circular shape they leave
> the silhouette and the mask cuts them. Hence `radiusAtAngle`.

This is the thing that makes a morph read as *3D* rather than as a 2D shape
scaling: the eyes are anchored to a sphere, and the body's actual local
radius positions them. Addled has no equivalent — eyes are drawn relative to
a fixed centre.

### 6. `sample(t)` is pure, so every frame is reproducible

> `engine.sample(t)` is a pure function of time. That is what makes the
> frozen state board and the DOM-less tests possible.

No hidden state, no `Date.now()`. That is how they can test 1921 frames and
lock "does not tremble" as an assertion.

---

## Where Addled is genuinely ahead

Not everything is a gap. Real wins, from reading both:

1. **It is alive in ways Grok's public engine is not.** Addled has 12 states,
   a separate continuous **emotion layer** that blends at its own attack
   rate, cursor gaze **plus** an idle saccade drift kept deliberately
   separate, blink, and a mood tint. The comments show real care — e.g. the
   reasoning for *not* blending the body colour toward warm, because on
   default blue it passed through green.

2. **It runs on the desktop, not in a page.** A frameless, always-on-top,
   translucent `QWidget` that wanders a corner, is draggable, and reacts to
   real OS state. Grok's is a web component. Different problem, different
   constraints — Addled's is harder on the integration axis.

3. **Paint errors cannot kill the app** (`paintEvent` wraps `_paint` in
   `try/except` because "PyQt6 aborts the app on exceptions inside
   paintEvent"). Grok's doc puts the equivalent care into the eye solver.

4. **A sprite-skin escape hatch.** `sprite_skin.py` lets a user drop in a
   codex-pet-style GIF, with per-state frame rows. Addled can be *not*
   procedural. Grok's customisation is still shapes.

---

## What would actually close the gap

Ranked by life-gained per unit of work. None of these is "make it 3D".

### 1. Move the body from a formula to a profile (the foundation)

Replace `draw_blob`'s closed form with the Grok model: a `number[N]` array of
radii. This is the *only* item that unlocks the others. It is not a rewrite —
`shapes.py` already builds a `QPainterPath` point by point:

```python
for i in range(steps + 1):
    x = cx + rx * math.copysign(abs(ct) ** (2.0 / n), ct)
    y = cy + ry * math.copysign(abs(st) ** (2.0 / n), st)
    path.lineTo(x, y)
```

The loop already exists. Storing `radii[i]` instead of computing `x` from
`n`, and lerping two arrays, is a contained change. Keep `n = 2.2` as the
*generator* for the default profile, so nothing regresses visually.

### 2. Give states a silhouette

Once (1) exists, the cheapest high-value win: let a few states carry their
own profile and morph. `THINKING` could pull toward a taller egg, `ERROR`
toward a flat wide squashed profile, `SLEEP` toward a settled low blob. This
is what makes a state *feel* different rather than just glow differently.

### 3. Squash and stretch on Y

Addled's `pulse_scale` inflates uniformly. Splitting it into `scale_x` /
`scale_y` and driving them inversely (volume-preserving) is the classic
animation principle and is a handful of lines — `painter.scale(sx, sy)`
already takes two arguments. This alone kills the "balloon" look, and it is
independent of (1), so it can be tried first and cheaply.

### 4. Tint the eyes to the body radius

Only worth it after (1), and only if shapes become non-circular. Otherwise
there is nothing to clip against.

### Deliberately *not* worth doing

- **3D.** The Grok companions that were 3D are being retired; the icon
  everyone likes is 2D with a fake-3D eye sphere. Addled's widget is a
  desktop overlay — a 3D scene is the wrong cost for a 64px character.
- **Porting the eye-offset solver.** That whole 18 KB `eyefit.ts` exists to
  fix a problem Addled does not have yet (customiser shapes making eyes
  overflow). It is a *consequence* of item 1, not a prerequisite.

---

## The honest summary

Addled's character is **more emotionally complex and less physically alive**
than Grok's. It has moods, gaze, drift, blinks, twelve states — but the body
those belong to cannot change shape. It inflates like a balloon and never
squashes.

Grok's is the opposite bet: a nearly inert emotional model on top of a body
that is a **morphable radial profile**, so every state is a silhouette and
every transition is a `lerp`.

The fix is not "animate more". It is to change the body's
**representation** — from a formula evaluated per frame to an array
interpolated per frame. Everything else follows from that one change, and
the loop to build the path already exists in the code.
