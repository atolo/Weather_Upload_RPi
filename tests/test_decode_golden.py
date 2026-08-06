"""Golden-master tests for WU_decodeData (AUDIT_PLAN stage R1).

Run from the repo root:  python3 -m pytest tests/ -q

The decode formulas are empirically tuned against this specific station and there is no
specification to rebuild them from. These tests freeze what the code does *today*, correct or
not, so that the refactor cannot silently change the numbers this station publishes.

They are characterisation tests, not correctness tests. Where current behaviour looks wrong,
the expected value records that it is wrong and a comment says so. Do not "fix" a failure here
by regenerating the golden file -- see tools/regen_golden.py.

Corpus: 2,125 distinct packets captured from the production station between 2026-02-17 and
2026-08-06. See tests/fixtures/packets.txt for provenance.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import WU_decodeData as decode
from tools.regen_golden import GOLDEN, decode_row, load_packets


def load_golden():
    rows = []
    with open(GOLDEN) as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if fields[0] == "packet":
                continue
            rows.append(fields)
    return rows


PACKETS = load_packets()
GOLDEN_ROWS = load_golden()


class TestCorpusIntegrity:
    def test_corpus_is_not_empty(self):
        assert len(PACKETS) > 2000

    def test_every_packet_is_eight_bytes(self):
        assert all(len(p) == 8 for p in PACKETS)

    def test_golden_covers_every_packet(self):
        assert len(GOLDEN_ROWS) == len(PACKETS)

    def test_every_packet_type_this_station_emits_is_represented(self):
        """A corpus missing a type would let a refactor break it undetected.

        This is the exact set observed in 2,933 samples spanning 2026-02-17 to 2026-08-06.
        If a future capture sees something outside it, the corpus is stale, not this list."""
        present = {p[0] >> 4 for p in PACKETS}
        assert present == {0x3, 0x4, 0x5, 0x6, 0x8, 0x9, 0xA, 0xB, 0xD, 0xE}

    def test_cap_volts_and_wind_speed_slots_are_never_transmitted(self):
        """0x1 and 0x2 do not appear once in 2,933 samples over roughly six months.

        Consistent with the hardware: the 6322C is a *wired* ISS powered over the RJ11, so it
        has no solar panel and no supercapacitor to report on -- capVoltage()'s own comment
        says "Vue only". Wind speed needs no dedicated message because it rides in byte 1 of
        every packet.

        Recorded rather than acted on. The corpus is outage-biased (one sample per minute, only
        while uploads are failing), so absence here is strong evidence but not proof, and this
        is precisely the inference that was got wrong about solar and UV. **R6 should leave the
        ISS_CAP_VOLTS branch in place**: keeping it costs six lines, while deleting it would --
        if a 0x2 ever did arrive -- drop that packet into the "Unhandled data type" path,
        returning decodeStatus=False and suppressing the log and upload for that cycle.

        Consequence to be aware of: capVoltage() therefore has no golden coverage, so a
        refactor touching it is unprotected."""
        assert not any(p[0] >> 4 in (0x1, 0x2) for p in PACKETS)


class TestGoldenMaster:
    def test_no_decoder_output_has_changed(self):
        """The whole point. Any diff means the published numbers moved."""
        mismatches = []
        for packet, expected in zip(PACKETS, GOLDEN_ROWS):
            actual = decode_row(packet)
            if actual != expected:
                mismatches.append(f"  {expected[0]}\n    expected {expected[1:]}\n    actual   {actual[1:]}")

        assert not mismatches, (
            f"{len(mismatches)} packet(s) decode differently than the golden master:\n"
            + "\n".join(mismatches[:10])
            + ("\n  ..." if len(mismatches) > 10 else "")
        )


class TestLoadBearingBehaviour:
    """Specific behaviours that must survive the refactor, called out because their importance
    is not obvious from reading the code."""

    def test_sensor_offline_solar_returns_zero_and_is_accepted(self):
        """This station has no solar sensor, but the ISS transmits the 0x6 slot anyway with
        0xFF in byte 3. The decoder's SENSOR_OFFLINE branch returns 0 and Weather_Station's
        `>= 0` guard accepts it. Deleting the 0x6 handling as fork residue -- which the audit's
        old 'never transmits' claim invited -- would send ~12% of packets to the 'Unhandled
        data type' branch, returning decodeStatus=False and suppressing the log and upload on
        those cycles. See AUDIT_PLAN items 8, 19 and the R6 warning."""
        solar = [p for p in PACKETS if p[0] >> 4 == 0x6]
        assert solar, "corpus lost its solar packets"
        assert all(p[3] == decode.SENSOR_OFFLINE for p in solar)
        assert all(decode.solarRadiation(p) == 0 for p in solar)
        assert all(decode.solarRadiation(p) >= 0 for p in solar), "must pass Weather_Station's guard"

    def test_sensor_offline_uv_returns_zero_and_is_accepted(self):
        """Same as solar, for the 0x4 slot. See AUDIT_PLAN item 9."""
        uv = [p for p in PACKETS if p[0] >> 4 == 0x4]
        assert uv, "corpus lost its UV packets"
        assert all(p[3] == decode.SENSOR_OFFLINE for p in uv)
        assert all(decode.uvIndex(p) == 0 for p in uv)
        assert all(decode.uvIndex(p) >= 0 for p in uv), "must pass Weather_Station's guard"

    def test_corrupt_packets_still_fail_crc(self):
        """Four packets in the corpus are genuinely corrupt. A refactor that made CRC more
        permissive would feed garbage into the decoders, and the CRC path is the only thing
        standing between a noisy RS485 line and the public feed."""
        failures = [p for p in PACKETS if not decode.crc16_ccitt(p)]
        assert len(failures) == 4
        assert {p[0] >> 4 for p in failures} >= {0x3, 0xB, 0xD}

    def test_rain_counter_stays_within_its_rollover_range(self):
        """Weather_Station rejects anything outside 0-127 and treats a decrease as a rollover.
        A decoder change that widened this range would corrupt the daily rain total."""
        counts = [decode.rainCounter(p) for p in PACKETS if p[0] >> 4 == 0xE]
        assert counts
        assert all(0 <= c <= 127 for c in counts)

    def test_wind_speed_is_always_an_integer(self):
        """windSpeed returns rawData[1] verbatim. This is what proved Weather Underground
        averages within its 5-minute bucket rather than storing our last sample: WU reported
        1.2 mph, and this station cannot emit a fractional wind speed. R5 depends on it."""
        assert all(isinstance(decode.windSpeed(p), int) for p in PACKETS)

    @pytest.mark.parametrize("header,name", [
        (0x5, "rainRate"), (0x8, "temperature"),
        (0x9, "windGusts"), (0xA, "humidity"),
    ])
    def test_decoders_reject_the_wrong_packet_type(self, header, name):
        """Every typed decoder guards on the header nibble. decodeRawData relies on it."""
        function = getattr(decode, name)
        wrong = next(p for p in PACKETS if p[0] >> 4 != header)
        assert function(wrong) == decode.ERR_WRONG_PACKET
