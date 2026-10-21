"""Per-project cost log: every billable call a worker step makes (Anthropic, Google, Modal compute).
jobs.run flushes USAGE onto pipeline_jobs.usage after each step; the edge function pipeline-banana appends Gemini directly."""
USAGE = []

GEOCODE_USD = 0.005                     # Google Geocoding API, $5 / 1,000 (Essentials, beyond the monthly free cap)
MODAL_CORE_S, MODAL_GIB_S = 0.0000131, 0.00000222   # modal.com/pricing, Oct 2026


def add(call, provider, cost_usd, **extra):
    USAGE.append({"call": call, "provider": provider, "cost_usd": round(float(cost_usd), 4), **extra})


def modal_compute(step, seconds, cores, gib):
    add(f"Compute: {step}", "Modal", seconds * (cores * MODAL_CORE_S + gib * MODAL_GIB_S),
        seconds=round(seconds, 1), cores=cores, mem_gib=gib)
