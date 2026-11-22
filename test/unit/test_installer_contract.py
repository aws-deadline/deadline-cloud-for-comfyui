# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Verify the public InstallBuilder interface without requiring a native artifact."""

from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
COMPONENT_NAME = "deadline_cloud_for_comfyui"


def test_public_component_can_be_selected_by_a_standalone_wrapper() -> None:
    project = ET.parse(ROOT / "installer/DeadlineCloudForComfyUISubmitter.xml").getroot()
    component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
    assert component.findtext("name") == COMPONENT_NAME
    assert {name: component.findtext(name) for name in ("selected", "show", "canBeEdited")} == {
        "selected": "0",
        "show": "1",
        "canBeEdited": "1",
    }
    assert project.findtext("allowComponentSelection") == "1"
    assert (
        project.findtext("componentList/include/file")
        == "../install_builder/deadline-cloud-for-comfyui.xml"
    )
    assert project.findtext("preInstallationActionList/componentSelection/select") == COMPONENT_NAME
    standalone = project.find(
        "preInstallationActionList/setInstallerVariable[@name='individual_install']"
    )
    assert standalone is not None and standalone.get("value") == "1"


def test_host_picker_and_unattended_options_belong_to_the_component() -> None:
    project = ET.parse(ROOT / "installer/DeadlineCloudForComfyUISubmitter.xml").getroot()
    component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
    group = component.find("parameterList/parameterGroup")
    assert group is not None and group.findtext("ask") == "1"
    source = group.find("parameterList/directoryParameter[name='comfyui_directory']")
    base = group.find("parameterList/directoryParameter[name='comfyui_base_directory']")
    assert source is not None and base is not None
    assert source.findtext("default") == "${user_home_directory}/ComfyUI"
    assert source.findtext("ask") == "1" and source.findtext("allowEmptyValue") == "0"
    assert source.findtext("cliOptionName") == "comfyui-directory"
    assert base.findtext("cliOptionName") == "comfyui-base-directory"
    assert base.findtext("allowEmptyValue") == "1"
    nodes = component.find("parameterList/stringParameter[@name='custom_nodes_directory']")
    assert nodes is not None and nodes.get("cliOptionName") == "comfyui-custom-nodes-directory"
    assert nodes.get("ask") == "0"
    prefix = project.find("parameterList/directoryParameter[name='installdir']")
    assert prefix is not None and prefix.findtext("cliOptionName") == "prefix"
    assert prefix.findtext("default") == "${user_home_directory}/DeadlineCloudForComfyUISubmitter"
    assert prefix.findtext("ask") == "0"
    global_names = {
        parameter.get("name") or parameter.findtext("name")
        for parameter in project.findall("parameterList/*")
    }
    assert (
        not {
            "comfyui_directory",
            "comfyui_base_directory",
            "custom_nodes_directory",
            "installscope",
            "comfyui_discovery_home",
        }
        & global_names
    )


def test_component_packages_a_private_runtime_and_uses_the_resolved_active_nodes() -> None:
    component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
    folders = {folder.findtext("name"): folder for folder in component.findall("folderList/folder")}
    assert folders["deadline_runtime"].findtext("destination") == "${installdir}/runtime"
    assert folders["comfyui_payload"].findtext("destination") == "${installdir}/payload"
    assert folders["installation_helper"].findtext("destination") == "${installdir}"
    hooks = component.findall("postInstallationActionList/runProgram")
    install_hook = next(
        hook for hook in hooks if " install " in (hook.findtext("programArguments") or "")
    )
    arguments = install_hook.findtext("programArguments") or ""
    assert '--comfyui "${deadline_comfyui_source}"' in arguments
    assert '--custom-nodes "${custom_nodes_directory}"' in arguments
    assert '--runtime "${installdir}/runtime"' in arguments
    assert '--state "${installdir}/installation.json"' in arguments
    assert install_hook.findtext("abortOnError") == "1"
    uninstaller = component.find("preUninstallationActionList/runProgram")
    assert uninstaller is not None
    assert 'uninstall --state "${installdir}/installation.json"' in (
        uninstaller.findtext("programArguments") or ""
    )
    assert uninstaller.findtext("abortOnError") == "1"
