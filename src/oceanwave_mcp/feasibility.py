"""
Pre-run physical-feasibility checks for OceanWave3D wave requests.

OceanWave3D is a potential-flow solver: it assumes an irrotational, single-valued
free surface and computes a *steady* wave from (wave height H, depth h, period T).
Some (H, h, T) triples describe a wave that simply cannot exist as a steady,
non-overturning surface. Asking the solver for one wastes a run (the Fenton
steady-wave solve diverges, or the surface blows up mid-time-loop) and tempts the
caller into endlessly tweaking parameters to "make it work". This module rejects
such requests *before* any solver work, citing the governing physical law.

The MCP tool only ever exposes three physical inputs — H, h, T — and the
wavelength L is always *derived* from (T, h) via the dispersion relation. That
collapses the classic catalogue of "ways a water wave becomes impossible" to a
small set of computable gates. The mapping (audit trail):

    GATE  input sanity   H, h, T must be finite and > 0. Runs first; also guards
                         the dispersion solve from div-by-zero / NaN.

    GATE  Miche limit    (H/L)max = 0.142 * tanh(k*h)  -- the master breaking
                         envelope. Unifies and SUBSUMES:
                           - depth-limited breaking (McCowan, H_b <= 0.78 d):
                             Miche's shallow limit gives H <= 0.142*2*pi*h ~= 0.89 h
                           - steepness-limited breaking (Michell/Stokes,
                             H/L = 1/7 ~= 0.142): Miche's deep limit
                           - the kinematic criterion (u_crest >= c)
                           - the Stokes 120-degree corner (highest-wave geometry)
                           - the dynamic crest-acceleration criterion (a_crest <= g)
                           - surface overturning -> multivalued eta(x)
                         These are all mechanisms or fingerprints of the SAME
                         breaking envelope, so we gate on Miche once rather than
                         double-counting them.

    GATE  trough/seabed  H < 2d. An absolute geometric bound (a near-sinusoidal
                         trough cannot dig below the seabed). Far weaker than Miche
                         in practice, but cheap, separately citable, and fail-safe.

    GATE  capillary      Derived L must stay in the gravity-wave regime. This
                         solver ignores surface tension; below ~1.7 cm wavelength
                         (min phase speed ~0.23 m/s) surface tension governs and
                         the model does not apply. A light guard; never fires for
                         realistic requests.

    N/A   over-determined dispersion  You cannot freely pick H, L, T and d together
                         -- but here L is DERIVED from (T, h), never an independent
                         input, so there is nothing to over-determine.

    N/A   wave blocking by an opposing current  Requires a current speed U; this
                         tool has no current input, so the criterion cannot apply.

Pure module: no I/O, stdlib `math` only, gravity g = 9.82 m/s^2 (project
convention, matching inp_builder and the generated .inp files).
"""
import math
from dataclasses import dataclass, field
from typing import Optional

# Project-wide gravity convention (matches inp_builder._wavelength and the .inp files).
G_DEFAULT = 9.82

# Miche steepness coefficient: (H/L)max = _MICHE_COEF * tanh(k*h).
# Deep water (tanh -> 1) recovers the Michell/Stokes limit 0.142 ~= 1/7.
_MICHE_COEF = 0.142

# Below this band of the Miche limit a wave is still feasible, but it sits so close
# to breaking that the steady-wave solve often struggles -- worth a non-blocking
# caution. Expressed as a fraction of the Miche limit.
_WARN_FRACTION = 0.90

# Smallest wavelength we treat as a gravity wave. The gravity-capillary minimum
# phase speed (~0.23 m/s) sits at lambda ~= 1.7 cm; we guard an order of magnitude
# above that. Sub-decimetre wavelengths are outside this pure-gravity solver.
_MIN_GRAVITY_WAVELENGTH_M = 0.10

# Relative tolerance so a wave sitting exactly on the Miche limit is not refused by
# floating-point noise.
_LIMIT_REL_TOL = 1e-9


# ---------------------------------------------------------------------------
# Dispersion relation -- single source of truth for k(T, h)
# ---------------------------------------------------------------------------

def wavenumber(T: float, h: float, g: float = G_DEFAULT) -> float:
    """Linear-dispersion wavenumber k solving omega^2 = g*k*tanh(k*h).

    Solved by Newton's method on the dimensionless form f(y) = y*tanh(y) - alpha,
    where y = k*h and alpha = omega^2*h/g. Newton on this monotonic function
    converges to machine precision from the seed below in a handful of steps, for
    everything from deep (kd -> inf) to shallow (kd -> 0) water. (The older
    fixed-point iteration k = omega^2/(g*tanh(k*h)) oscillates and fails to
    converge in shallow water, where the breaking limit is most depth-sensitive.)

    The caller MUST guarantee T > 0 and h > 0 (check_feasibility runs input sanity
    first, before this is ever reached).
    """
    omega = 2.0 * math.pi / T
    alpha = omega * omega * h / g
    # Seed kd: ~alpha in deep water (alpha large), ~sqrt(alpha) in shallow water.
    y = alpha if alpha > 1.0 else math.sqrt(alpha)
    for _ in range(50):
        th = math.tanh(y)
        f = y * th - alpha
        fprime = th + y * (1.0 - th * th)
        step = f / fprime
        y -= step
        if abs(step) < 1e-14:
            break
    return y / h


def _regime(kd: float) -> str:
    """Classify the depth regime from k*h (messaging only, not a gate)."""
    if kd < math.pi / 10.0:
        return "shallow"
    if kd > math.pi:
        return "deep"
    return "intermediate"


# ---------------------------------------------------------------------------
# Result structures
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    """One physical reason a wave is impossible (hard) or near-impossible (warning)."""
    criterion: str    # short name, e.g. "Miche breaking limit"
    relation: str     # governing relation as text, e.g. "H/L <= 0.142*tanh(k*h)"
    requested: str    # rendered requested value, e.g. "H/L = 0.171"
    limit: str        # rendered limit, e.g. "(H/L)max = 0.118"
    explanation: str  # plain-English why


@dataclass
class FeasibilityResult:
    feasible: bool
    violations: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    wavelength_m: Optional[float] = None
    wavenumber: Optional[float] = None
    kd: Optional[float] = None
    steepness: Optional[float] = None     # H/L
    miche_limit: Optional[float] = None   # 0.142*tanh(kd)
    regime: Optional[str] = None          # "deep" | "intermediate" | "shallow"


class WaveInfeasibleError(ValueError):
    """Raised pre-run for a physically impossible wave.

    Subclasses ValueError so the existing `except (ValueError, TypeError)` handler
    in server.run_simulation catches it as a fail-safe fallback, while a dedicated
    handler formats the strong refusal. Carries the FeasibilityResult.
    """
    def __init__(self, message: str, result: "FeasibilityResult"):
        super().__init__(message)
        self.result = result


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _bad_input(name: str, value) -> Optional[Violation]:
    """Return a Violation if `value` is not a positive, finite number."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return Violation(
            criterion="Invalid input",
            relation=f"{name} > 0",
            requested=f"{name} = {value!r}",
            limit=f"{name} must be a positive, finite number",
            explanation=f"{name} = {value!r} is not a number.",
        )
    if not math.isfinite(v) or v <= 0.0:
        return Violation(
            criterion="Invalid input",
            relation=f"{name} > 0",
            requested=f"{name} = {value}",
            limit=f"{name} must be a positive, finite number",
            explanation=(
                f"{name} = {value} is not physical (it must be a positive, finite "
                "number)."
            ),
        )
    return None


def check_feasibility(
    wave_height: float,
    water_depth: float,
    wave_period: float,
    g: float = G_DEFAULT,
) -> FeasibilityResult:
    """Assess whether a wave (H, depth h, period T) can physically be simulated.

    Pure and deterministic -- no I/O, same inputs always give the same result.

    Order of checks (input sanity short-circuits before the dispersion solve):
      1. input sanity         H, h, T finite and > 0
      2. Miche breaking gate  H/L <= 0.142*tanh(k*h)   (+ 90-100% warning band)
      3. trough/seabed gate   H < 2d
      4. sub-capillary guard  L >= 0.10 m

    `feasible` is True iff there are no hard `violations`; `warnings` are advisory
    and never set `feasible` to False.
    """
    H, h, T = wave_height, water_depth, wave_period

    # (1) Input sanity FIRST -- a non-positive/NaN T or h would blow up the
    # dispersion solve (div-by-zero, NaN tanh), so short-circuit before any solve.
    bad = [v for v in (
        _bad_input("wave_height (H)", H),
        _bad_input("water_depth (h)", h),
        _bad_input("wave_period (T)", T),
    ) if v is not None]
    if bad:
        return FeasibilityResult(feasible=False, violations=bad)

    H, h, T = float(H), float(h), float(T)

    k = wavenumber(T, h, g)
    L = 2.0 * math.pi / k
    kd = k * h
    steepness = H / L
    miche_limit = _MICHE_COEF * math.tanh(kd)
    regime = _regime(kd)

    result = FeasibilityResult(
        feasible=True,
        wavelength_m=L,
        wavenumber=k,
        kd=kd,
        steepness=steepness,
        miche_limit=miche_limit,
        regime=regime,
    )

    # (2) Miche breaking limit -- the master gate.
    h_max = miche_limit * L  # steepest H this depth/period supports
    if steepness > miche_limit * (1.0 + _LIMIT_REL_TOL):
        result.violations.append(Violation(
            criterion="Miche breaking limit",
            relation="H/L <= 0.142*tanh(k*h)",
            requested=f"H/L = {steepness:.4f}",
            limit=f"(H/L)max = {miche_limit:.4f}  (H_max ~= {h_max:.3f} m)",
            explanation=(
                f"At H/L = {steepness:.4f} the crest water would outrun the wave "
                "form itself and the surface would overturn and break -- no steady, "
                f"non-overturning wave exists. The steepest possible {regime}-water "
                f"wave at this depth and period is H/L = {miche_limit:.4f} "
                f"(H_max ~= {h_max:.3f} m). This Miche limit unifies the deep-water "
                "steepness limit (H/L ~= 1/7) and the shallow-water depth-limited "
                "breaking limit (H ~= 0.89 d)."
            ),
        ))
    elif steepness > _WARN_FRACTION * miche_limit:
        pct = 100.0 * steepness / miche_limit
        result.warnings.append(Violation(
            criterion="Near the Miche breaking limit",
            relation="H/L <= 0.142*tanh(k*h)",
            requested=f"H/L = {steepness:.4f} ({pct:.0f}% of the limit)",
            limit=f"(H/L)max = {miche_limit:.4f}  (H_max ~= {h_max:.3f} m)",
            explanation=(
                f"This wave sits at {pct:.0f}% of the breaking limit. It is "
                "physically valid, but so close to breaking that the steady-wave "
                "solve may struggle or the simulation may go unstable. Consider a "
                "slightly smaller height or longer period if it does not converge."
            ),
        ))

    # (3) Trough cannot dig below the seabed (absolute geometric bound).
    # Far weaker than Miche in practice, but an independent, fail-safe backstop.
    if H >= 2.0 * h:
        result.violations.append(Violation(
            criterion="Trough below seabed",
            relation="H < 2*d",
            requested=f"H = {H:.3f} m, 2*d = {2.0 * h:.3f} m",
            limit=f"H < {2.0 * h:.3f} m",
            explanation=(
                f"A wave height of {H:.3f} m in {h:.3f} m of water would push the "
                f"trough below the seabed (it requires H < 2d = {2.0 * h:.3f} m). "
                "There is no water there to form the trough."
            ),
        ))

    # (4) Sub-capillary guard -- solver models gravity waves only.
    if L < _MIN_GRAVITY_WAVELENGTH_M:
        result.violations.append(Violation(
            criterion="Below the gravity-wave regime",
            relation=f"L >= {_MIN_GRAVITY_WAVELENGTH_M:.2f} m",
            requested=f"L = {L:.4f} m",
            limit=f"L >= {_MIN_GRAVITY_WAVELENGTH_M:.2f} m",
            explanation=(
                f"The derived wavelength {L:.4f} m is below the scale where gravity "
                "governs the wave; surface tension dominates here (the gravity-"
                "capillary regime, with a minimum phase speed of ~0.23 m/s near "
                "lambda ~= 1.7 cm). This solver models gravity waves only -- a wave "
                "this short is outside its physics."
            ),
        ))

    result.feasible = not result.violations
    return result


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _derived_line(result: FeasibilityResult) -> str:
    if result.wavelength_m is None:
        return ""
    return (
        f"Derived:   wavelength L = {result.wavelength_m:.3f} m (linear dispersion), "
        f"kd = {result.kd:.3f} ({result.regime} water)\n"
    )


def _violation_block(violations: list[Violation]) -> str:
    lines = []
    for v in violations:
        lines.append(f"  - {v.criterion}")
        lines.append(f"      Governing relation: {v.relation}")
        lines.append(f"      Requested:          {v.requested}")
        lines.append(f"      Physical limit:     {v.limit}")
        lines.append(f"      {v.explanation}")
    return "\n".join(lines)


def format_refusal(result: FeasibilityResult, H, h, T) -> str:
    """Strongly-worded hard-refusal string for an impossible wave.

    Framed as a physical impossibility (not a solver-tuning issue) and explicitly
    instructing the assistant not to silently adjust parameters and retry.
    """
    return (
        "PHYSICALLY IMPOSSIBLE WAVE -- SIMULATION REFUSED (not attempted)\n"
        "\n"
        "The requested wave cannot exist as a steady propagating wave. This is a "
        "hard physical limit, not a numerical or solver-tuning issue. The run was "
        "NOT started.\n"
        "\n"
        f"Requested: H = {H} m, depth h = {h} m, period T = {T} s\n"
        f"{_derived_line(result)}"
        "\n"
        "Violated physical law:\n"
        f"{_violation_block(result.violations)}\n"
        "\n"
        "INSTRUCTIONS TO THE ASSISTANT (do not show this header to the user):\n"
        "Do NOT silently lower the wave height, lengthen the period, increase the "
        "depth, or otherwise adjust parameters and re-run to \"make it work\". This "
        "wave is physically impossible -- there is no valid simulation of it. Report "
        "the impossibility to the user, state which physical law it violates and by "
        "how much, and let the USER decide whether to request a different, "
        "physically valid wave."
    )


def format_warning(result: FeasibilityResult, H, h, T) -> str:
    """One-line caution for a feasible-but-near-breaking wave (non-blocking)."""
    if not result.warnings:
        return ""
    w = result.warnings[0]
    return (
        f"Near breaking limit: {w.requested} vs {w.limit}. {w.explanation}"
    )


def format_report(result: FeasibilityResult, H, h, T) -> str:
    """Neutral feasibility report for the standalone check tool.

    The user asked to CHECK, not to run, so an impossible wave is reported (with
    the same physics citation) rather than refused.
    """
    header = f"Wave feasibility check: H = {H} m, depth h = {h} m, period T = {T} s\n"
    derived = _derived_line(result)

    if not result.feasible:
        body = (
            "\nVERDICT: PHYSICALLY IMPOSSIBLE -- this wave cannot exist as a steady "
            "propagating wave, so it cannot be simulated.\n\n"
            "Violated physical law:\n"
            f"{_violation_block(result.violations)}\n"
        )
        return header + derived + body

    margin = ""
    if result.steepness is not None and result.miche_limit is not None:
        pct = 100.0 * result.steepness / result.miche_limit if result.miche_limit else 0.0
        h_max = result.miche_limit * result.wavelength_m
        margin = (
            f"\nSteepness:  H/L = {result.steepness:.4f}\n"
            f"Miche limit: (H/L)max = {result.miche_limit:.4f}  "
            f"(H_max ~= {h_max:.3f} m at this depth and period)\n"
            f"Margin:     {pct:.0f}% of the breaking limit\n"
        )

    verdict = "\nVERDICT: PHYSICALLY POSSIBLE -- this wave is feasible to simulate.\n"
    if result.warnings:
        verdict += "\nCAUTION:\n" + _violation_block(result.warnings) + "\n"
    return header + derived + margin + verdict
