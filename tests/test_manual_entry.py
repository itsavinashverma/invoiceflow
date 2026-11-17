import pandas as pd

from invoiceflow.schemas import InvoiceExtraction, LineItemExtraction
from invoiceflow.ui.manual_entry import dataframe_to_lines, lines_to_dataframe


def test_blank_and_nan_rows_are_dropped():
    df = pd.DataFrame(
        [
            {"description": "Widget", "item_code": "", "quantity": "2", "unit_price": "1.50", "line_total": "3.00"},
            {"description": "", "item_code": None, "quantity": float("nan"), "unit_price": "", "line_total": " "},
        ]
    )
    lines = dataframe_to_lines(df)
    assert len(lines) == 1 and lines[0].description == "Widget" and lines[0].item_code is None
    assert lines[0].line_total == "3.00"  # kept as the string the user typed


def test_prefill_round_trip():
    ex = InvoiceExtraction(line_items=[LineItemExtraction(description="A", quantity="1", unit_price="2.00", line_total="2.00")])
    assert dataframe_to_lines(lines_to_dataframe(ex)) == ex.line_items
    assert dataframe_to_lines(lines_to_dataframe(None)) == []
