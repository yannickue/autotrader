"""C4: instrument mapping from the REAL symbol_info snapshot + deterministic catalog."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from data.historical import read_bar_dataset
from data.provenance import SemanticType
from nautilus_kernel.catalog import (
    bar_close_ns,
    bar_type_strings,
    read_catalog,
    records_to_nautilus_bars,
    write_catalog,
)
from nautilus_kernel.instrument import (
    INSTRUMENT_ASSUMPTIONS,
    load_symbol_info_snapshot,
    spec_to_nautilus_cfd,
)


@pytest.fixture(scope="module")
def snapshot(symbol_info_path):
    return load_symbol_info_snapshot(symbol_info_path)


@pytest.fixture(scope="module")
def instrument(snapshot):
    return spec_to_nautilus_cfd(snapshot.spec)


def test_instrument_fields_come_from_real_broker_snapshot(snapshot, instrument):
    assert snapshot.account_kind == "DEMO"
    assert str(instrument.id) == "GER40.ACTIVTRADES"
    assert str(instrument.raw_symbol) == "Ger40"  # canonical id vs broker symbol kept apart
    assert (instrument.price_precision, float(instrument.price_increment)) == (2, 0.01)
    assert (instrument.size_precision, float(instrument.size_increment)) == (2, 0.25)
    assert float(instrument.min_quantity) == 0.25 and float(instrument.max_quantity) == 250.0
    assert str(instrument.quote_currency) == "EUR"
    assert float(instrument.multiplier) == 1.0
    assert instrument.info["broker_symbol"] == "Ger40"
    assert instrument.info["stop_level_price"] == "1.00"  # 100 points * 0.01


def test_unprovided_fields_are_explicit_assumptions_not_silent(snapshot, instrument):
    assert snapshot.spec.margin_initial is None  # broker reported 0.0 => not provided
    assert instrument.margin_init == Decimal("0.05")
    assert set(INSTRUMENT_ASSUMPTIONS) >= {"margin_init", "fees", "swap", "contract_size"}
    assert instrument.info["assumptions"] == INSTRUMENT_ASSUMPTIONS


def test_unsupported_instrument_shapes_fail_closed(snapshot):
    with pytest.raises(ValueError, match="contract size"):
        spec_to_nautilus_cfd(replace(snapshot.spec, trade_contract_size=Decimal("10")))
    with pytest.raises(ValueError, match="not tradable"):
        spec_to_nautilus_cfd(replace(snapshot.spec, is_tradable=False))
    with pytest.raises(ValueError, match="currencies differ"):
        spec_to_nautilus_cfd(replace(snapshot.spec, currency_margin="USD"))


@pytest.fixture(scope="module")
def records(c3_dataset):
    recs, _ = read_bar_dataset(c3_dataset)
    return recs


def test_bars_close_stamped_and_ask_is_bid_plus_bar_spread(instrument, records, snapshot):
    bid, ask = records_to_nautilus_bars(records, instrument, snapshot.spec.point)
    assert len(bid) == len(ask) == len(records) == 1185
    first = records[0]
    assert bid[0].ts_event == bar_close_ns(first, "5m")
    open_ns = int(first.timestamp.timestamp()) * 1_000_000_000
    assert bid[0].ts_event - open_ns == 300 * 1_000_000_000  # close = open + 5m
    spread = Decimal(first.spread_points) * snapshot.spec.point
    assert Decimal(str(ask[0].close)) - Decimal(str(bid[0].close)) == spread
    assert Decimal(str(bid[0].close)) == first.close
    bid_s, ask_s = bar_type_strings(instrument, "5m")
    assert str(bid[0].bar_type) == bid_s and str(ask[0].bar_type) == ask_s


def test_bar_conversion_rejects_bad_semantics_and_missing_spread(instrument, records, snapshot):
    point = snapshot.spec.point
    with pytest.raises(ValueError, match="BROKER_TICK_ACTIVITY"):
        bad = replace(records[0], volume_semantic_type=SemanticType.REAL_EXCHANGE_VOLUME)
        records_to_nautilus_bars([bad, *records[1:3]], instrument, point)
    with pytest.raises(ValueError, match="spread"):
        records_to_nautilus_bars([replace(records[0], spread_points=None)], instrument, point)
    with pytest.raises(ValueError, match="increasing"):
        records_to_nautilus_bars([records[1], records[0]], instrument, point)
    with pytest.raises(ValueError, match="no bar records"):
        records_to_nautilus_bars([], instrument, point)


def test_catalog_write_read_roundtrip_is_exact_and_deterministic(
    instrument, records, snapshot, tmp_path: Path
):
    bid, ask = records_to_nautilus_bars(records, instrument, snapshot.spec.point)
    write_catalog(tmp_path / "a", instrument, bid, ask)
    write_catalog(tmp_path / "b", instrument, bid, ask)
    inst_a, bid_a, ask_a = read_catalog(tmp_path / "a", instrument, "5m")
    _inst_b, bid_b, ask_b = read_catalog(tmp_path / "b", instrument, "5m")
    assert inst_a == instrument and bid_a == bid and ask_a == ask
    assert (bid_a, ask_a) == (bid_b, ask_b)

    def tree(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.parquet"))}

    files_a, files_b = tree(tmp_path / "a"), tree(tmp_path / "b")
    assert files_a and files_a.keys() == files_b.keys()
    assert files_a == files_b  # byte-identical catalog files


def test_symbol_info_snapshot_is_real_capture(symbol_info_path):
    payload = json.loads(symbol_info_path.read_text())
    assert payload["symbol_info"]["path"] == "Cash Indices\\Ger40"
    assert payload["symbol_info"]["description"] == "DAX Cash Index"
