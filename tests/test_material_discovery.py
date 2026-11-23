from synty_mcp import unity


def test_duplicate_names_are_ambiguous_and_flat_slots_remain_flat(tmp_path):
    for directory in ("a", "b"):
        path = tmp_path / directory / "Atlas.png"
        path.parent.mkdir()
        path.touch()
    textures = unity.texture_index(tmp_path)
    result = unity.resolve_material_slots([
        {"material": "A", "texture": "Atlas"},
        {"material": "B", "texture": "Atlas", "untextured": True},
        {"material": "C", "texture": "Absent"},
    ], textures)
    assert result["slot_states"] == ["ambiguous", "untextured", "missing"]
    assert result["slots"] == [None, None, None]
    assert result["candidates"][0] == ["Atlas.png", "Atlas.png"]
    assert not result["by_material"]


def test_duplicate_material_name_with_different_slots_uses_slot_order(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    result = unity.resolve_material_slots([{"material": "Shared", "texture": "a"},
                                           {"material": "Shared", "texture": "b"}], {"a": [a], "b": [b]})
    assert result["slots"] == [str(a), str(b)]
    assert result["by_material"] == {} and result["material_states"] == {}


def test_material_does_not_use_normal_as_albedo(tmp_path):
    guid = "a" * 32
    image = tmp_path / "normal.png"
    mat = tmp_path / "material.mat"
    mat.write_text(f"m_TexEnvs:\n    - _BumpMap:\n        m_Texture: {{fileID: 2800000, guid: {guid}}}\n")
    assert unity.material_albedo(mat, {guid: image}) is None


def test_atlas_fallback_requires_unique_candidate(tmp_path):
    a = tmp_path / "one" / "Pack_Texture_01.png"
    b = tmp_path / "two" / "Pack_Texture_01.png"
    assert unity.default_atlas({"pack_texture_01": [a]}) == a
    assert unity.default_atlas({"pack_texture_01": [a, b]}) is None
