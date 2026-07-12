from livingpc import soul_calibration as calibration


def test_inventory_is_eight_balanced_sections_in_question_order():
    fields = calibration.FIELDS
    sections = calibration.sections_in_order()
    grouped = [
        [field for field in fields if field["section"] == section]
        for section in sections
    ]

    assert len(fields) == 31
    assert len(sections) == 8
    assert [len(group) for group in grouped] == [4, 4, 4, 4, 4, 4, 5, 2]
    assert len({calibration.field_key(field) for field in fields}) == 31
    assert [calibration.field_key(field) for group in grouped for field in group] == [
        calibration.field_key(field) for field in fields
    ]


def test_finishing_a_section_advances_to_the_next_section():
    fields = calibration.FIELDS
    answered = {calibration.field_key(field) for field in fields[:4]}

    assert calibration.next_field(answered) == fields[4]
    assert fields[4]["section"] == calibration.sections_in_order()[1]


def test_final_section_has_one_media_prompt_and_one_open_box():
    attributes = [field["attribute"] for field in calibration.FIELDS]

    assert attributes[-2:] == [
        "favorite media and creators",
        "other essential context",
    ]


def test_choice_questions_are_grouped_with_closing_reflection():
    fields = calibration.FIELDS
    choices = [field for field in fields if field["section_en"] == "Choices & Reflection"]

    assert len(choices) == 5
    assert [field["attribute"] for field in choices[-3:]] == [
        "most impactful life choice", "best life choice", "worst life choice and wisdom"
    ]


def test_prior_media_fields_resolve_to_the_new_combined_prompt():
    current = calibration.resolve_field(
        "Favorites & Open Space", "favorite media and creators")
    legacy = calibration.resolve_field("Style Anchors", "favorite movies")

    assert current is not None and legacy is not None
    assert calibration.field_key(current) == calibration.field_key(legacy)
