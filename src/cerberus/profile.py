"""House-bank voltage profile: every bank-side voltage threshold, derived from
the system's nominal voltage and battery chemistry (config.yaml `system`).

The analysis was written against this rig, a 12 V LiFePO4 bank, so its
thresholds were absolute 12 V figures. A 24 V or 48 V bank is the same cells
in longer strings (4 / 8 / 16 in series), so each figure scales by
nominal / 12 exactly. Values set explicitly in config.yaml still win; these
only fill what is left unset.

The vehicle's own chassis battery is 12 V whatever the house bank is, so the
OBD running threshold and the drive report's module-voltage limits are not
part of this.
"""

from __future__ import annotations

from dataclasses import dataclass

LIFEPO4 = "lifepo4"
CHEMISTRIES = (LIFEPO4,)
NOMINAL_VOLTAGES = (12, 24, 48)

# 12 V LiFePO4 (4 cells): the figures this rig's analysis was tuned on.
_LIFEPO4_12V = {
    # The packs' charge request and the MPPT setpoint here (3.60 V/cell).
    "absorption_v": 14.4,
    # At or above this, with SoC high and current tapered, counts as a true
    # full charge (3.50 V/cell).
    "full_min_v": 14.0,
    # A drive peaking here is past anything the packs asked for; 09-24 reached
    # 14.75 V with two CRIT alarms (3.65 V/cell).
    "peak_limit_v": 14.6,
    # Time above this counts as running past the 14.4 V request (3.6125 V/cell).
    "over_request_v": 14.45,
    # Charge at or above this is absorption into a full bank and legitimately
    # raises SoC by nothing, so SoC-drift accounting ignores it (3.55 V/cell).
    "bulk_max_v": 14.2,
    # Just above the Lithionics 12.1 V cutoff (3.075 V/cell).
    "crit_v": 12.3,
    # Outside this a reading does not belong to a bank of this nominal
    # voltage: the configured system voltage is wrong (2.5-3.75 V/cell).
    "plausible_min_v": 10.0,
    "plausible_max_v": 15.0,
}


@dataclass(frozen=True)
class VoltageProfile:
    nominal_v: int
    chemistry: str
    absorption_v: float
    full_min_v: float
    peak_limit_v: float
    over_request_v: float
    bulk_max_v: float
    crit_v: float
    plausible_min_v: float
    plausible_max_v: float

    def plausible(self, volts: float) -> bool:
        """Whether a live bank reading fits this profile's nominal voltage."""
        return self.plausible_min_v <= volts <= self.plausible_max_v


def profile(nominal_v: int = 12, chemistry: str = LIFEPO4) -> VoltageProfile:
    """The profile for a bank, or ValueError for one not supported yet."""
    if chemistry not in CHEMISTRIES:
        raise ValueError(
            f"chemistry {chemistry!r} is not supported yet (supported: {', '.join(CHEMISTRIES)})"
        )
    if nominal_v not in NOMINAL_VOLTAGES:
        raise ValueError(
            f"nominal voltage {nominal_v!r} must be one of "
            f"{', '.join(str(v) for v in NOMINAL_VOLTAGES)}"
        )
    k = nominal_v // 12
    return VoltageProfile(
        nominal_v=nominal_v,
        chemistry=chemistry,
        **{name: round(v * k, 4) for name, v in _LIFEPO4_12V.items()},
    )


DEFAULT_PROFILE = profile()
