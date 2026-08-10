from core.ai_label import _merge_ai_and_dictionary


def test_dictionary_metadata_is_authoritative_and_keeps_all_spans():
    text = "Đột quỵ não cần theo dõi; tiền sử đột quỵ não."
    dictionary = [
        {
            "term": "Đột quỵ não",
            "start": 0,
            "end": 12,
            "code": "I64",
            "label_vn": "Đột quỵ, không xác định do xuất huyết hay nhồi máu",
            "dictionary_type": "Bệnh Lý",
            "matched_by": "alias",
        },
        {
            "term": "đột quỵ não",
            "start": 37,
            "end": 49,
            "code": "I64",
            "label_vn": "Đột quỵ, không xác định do xuất huyết hay nhồi máu",
            "dictionary_type": "Bệnh Lý",
            "matched_by": "alias",
        },
    ]

    result = _merge_ai_and_dictionary(
        text,
        {"Triệu chứng": ["đột quỵ não"]},
        dictionary,
    )

    assert result["Triệu chứng"] == []
    assert len(result["Bệnh lý"]) == 1
    entity = result["Bệnh lý"][0]
    assert entity["code"] == "I64"
    assert entity["label_vn"] == "Đột quỵ, không xác định do xuất huyết hay nhồi máu"
    assert entity["source"] == "ai+dictionary"
    assert entity["spans"] == [{"start": 0, "end": 12}, {"start": 37, "end": 49}]


def test_hallucinated_ai_term_is_rejected():
    result = _merge_ai_and_dictionary(
        "Bệnh nhân đau đầu.",
        {"Bệnh lý": ["tăng huyết áp"], "Triệu chứng": ["đau đầu"]},
        [],
    )

    assert result["Bệnh lý"] == []
    assert result["Triệu chứng"][0]["term"] == "đau đầu"
    assert result["Triệu chứng"][0]["spans"] == [{"start": 10, "end": 17}]


def test_dictionary_result_survives_empty_ai_output():
    text = "Bệnh nhân tăng huyết áp."
    dictionary = [
        {
            "term": "tăng huyết áp",
            "start": 10,
            "end": 24,
            "code": "I10",
            "label_vn": "Tăng huyết áp nguyên phát",
            "dictionary_type": "Bệnh Lý",
            "matched_by": "exact",
        }
    ]

    result = _merge_ai_and_dictionary(text, {}, dictionary)

    assert result["Bệnh lý"][0]["code"] == "I10"
    assert result["Bệnh lý"][0]["source"] == "ai+dictionary"
