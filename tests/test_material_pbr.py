import pytest

from synty_mcp import unity


def test_unity_material_properties_and_missing_guid(tmp_path):
    guid = "a" * 32
    mat = tmp_path / "Example.mat"
    mat.write_text(
        "m_TexEnvs:\n    - _BaseMap:\n        m_Texture: {fileID: 2800000, guid: " + guid + "}\n"
        "    m_Floats:\n    - _Metallic: 0.6\n    - _Smoothness: 0.75\n"
        "    - _AlphaClip: 1\n    - _Cutoff: 0.3\n"
        "    m_Colors:\n    - _BaseColor: {r: 0.2, g: 0.4, b: 0.6, a: 0.8}\n"
        "    - _EmissionColor: {r: 0.1, g: 0.2, b: 0.3, a: 1}\n"
    )
    result = unity.material_properties(mat, {})
    assert result["state"] == "missing" and result["expected_texture"] == "GUID:" + guid
    assert result["properties"] == {"base_color": [0.2, 0.4, 0.6, 0.8], "emission": [0.1, 0.2, 0.3],
                                    "metallic": 0.6, "roughness": 0.25,
                                    "alpha_mode": "MASK", "alpha_cutoff": 0.3}
    image = tmp_path / "Atlas.png"
    image.touch()
    assert unity.material_properties(mat, {guid: image})["texture"] == str(image)
    image.unlink()
    missing = unity.material_properties(mat, {guid: image})
    assert missing["state"] == "missing" and missing["texture"] is None


def test_custom_shader_material_name_and_identical_copies(tmp_path):
    material_list = tmp_path / "MaterialList.txt"
    material_list.write_text("Prefab Name: P\n Mesh Name: M\n Slot: Atlas (Uses custom shader)\n")
    slots = unity.parse_material_list_meshes(material_list)["P"]["M"]
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    textures = unity.deduplicate_textures({"atlas": [a, b]})
    result = unity.resolve_material_slots(slots, textures)
    assert result["slot_states"] == ["resolved"]
    assert result["resolution_methods"] == ["material_name"]
    assert result["custom_shaders"] == [True]
    b.write_bytes(b"different")
    assert unity.resolve_material_slots(slots, unity.deduplicate_textures({"atlas": [a, b]}))["slot_states"] == ["ambiguous"]


@pytest.mark.blender
def test_glb_contains_approximation_and_embedded_atlas(svc, tmp_path):
    import json
    import struct
    import subprocess

    import fixtures
    from conftest import blender_path

    blender = blender_path()
    if not blender:
        pytest.skip("Blender not available")
    source, script, atlas = tmp_path / "source.blend", tmp_path / "make.py", tmp_path / "Atlas.png"
    atlas.write_bytes(fixtures.png_bytes())
    script.write_text(
        "import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n"
        "bpy.ops.mesh.primitive_cube_add()\nm=bpy.data.materials.new('Paint');m.use_nodes=True\n"
        "bpy.context.object.data.materials.append(m)\n"
        f"bpy.ops.wm.save_as_mainfile(filepath={str(source)!r})\n"
    )
    subprocess.run([blender, "-b", "--python", str(script)], check=True, capture_output=True, timeout=90)
    props = {"base_color": [0.2, 0.4, 0.6, 0.8], "metallic": 0.6, "roughness": 0.25,
             "emission": [0.1, 0.2, 0.3], "alpha_mode": "MASK", "alpha_cutoff": 0.3}
    textures = {"slots": [str(atlas)], "slot_states": ["resolved"], "unity_materials": [
        {"material": "Paint", "texture": str(atlas), "properties": props, "shader_approximation": "Unity PBR"}]}
    result = svc.converter.convert(pack_id="Synthetic", asset_id="PBR", src=source, node=None,
                                   textures=textures, log=lambda *_: None)
    data = result["path"]
    with open(data, "rb") as stream:
        stream.read(12)
        length, _ = struct.unpack("<II", stream.read(8))
        doc = json.loads(stream.read(length))
    material = doc["materials"][0]
    pbr = material["pbrMetallicRoughness"]
    assert pbr["baseColorTexture"]["index"] == 0 and "bufferView" in doc["images"][0]
    assert pbr["baseColorFactor"] == pytest.approx(props["base_color"], abs=0.01)
    assert pbr["metallicFactor"] == pytest.approx(0.6)
    assert pbr["roughnessFactor"] == pytest.approx(0.25)
    assert material["emissiveFactor"] == pytest.approx(props["emission"], abs=0.01)
    assert material["alphaMode"] == "MASK" and material["alphaCutoff"] == pytest.approx(0.3)
    assert result["textures"][0]["resolution_method"] == "unity_guid"
    assert any("shaders approximated" in warning for warning in result["warnings"])


@pytest.mark.blender
def test_unity_properties_remain_distinct_per_mesh(svc, tmp_path):
    import json
    import struct
    import subprocess

    from conftest import blender_path

    blender = blender_path()
    if not blender:
        pytest.skip("Blender not available")
    source, script = tmp_path / "source.blend", tmp_path / "make.py"
    script.write_text(
        "import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n"
        "m=bpy.data.materials.new('Shared');m.use_nodes=True\n"
        "for name in ['First','Second']:\n"
        " bpy.ops.mesh.primitive_cube_add();bpy.context.object.name=name\n"
        " bpy.context.object.data.materials.append(m)\n"
        f"bpy.ops.wm.save_as_mainfile(filepath={str(source)!r})\n"
    )
    subprocess.run([blender, "-b", "--python", str(script)], check=True, capture_output=True, timeout=90)

    def binding(roughness):
        return {"slots": [None], "slot_states": ["untextured"], "unity_materials": [{
            "material": "Shared", "texture": None, "properties": {"roughness": roughness},
            "shader_approximation": "Unity PBR"}]}

    result = svc.converter.convert(pack_id="Synthetic", asset_id="PerMesh", src=source, node=None,
                                   textures={"by_mesh": {"First": binding(0.2), "Second": binding(0.9)}},
                                   log=lambda *_: None)
    with open(result["path"], "rb") as stream:
        stream.read(12)
        length, _ = struct.unpack("<II", stream.read(8))
        doc = json.loads(stream.read(length))
    materials = {m["name"]: m["pbrMetallicRoughness"]["roughnessFactor"] for m in doc["materials"]}
    for row in result["textures"]:
        expected = 0.2 if row["mesh"] == "First" else 0.9
        assert materials[row["material"]] == pytest.approx(expected)
        assert row["properties"]["roughness"] == expected
