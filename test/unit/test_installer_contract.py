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
    source = component.find("parameterList/directoryParameter[name='comfyui_directory']")
    assert source is not None
    assert source.findtext("default") == "${user_home_directory}/ComfyUI"
    assert source.findtext("ask") == "1" and source.findtext("allowEmptyValue") == "0"
    assert source.findtext("cliOptionName") == "comfyui-directory"
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


def test_component_uses_the_selected_folder_and_existing_python() -> None:
    component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
    folders = {folder.findtext("name"): folder for folder in component.findall("folderList/folder")}
    assert set(folders) == {"comfyui_payload", "deadline_dependencies", "installation_helper"}
    assert folders["comfyui_payload"].findtext("destination") == "${installdir}/payload"
    assert folders["installation_helper"].findtext("destination") == "${installdir}"
    hooks = component.findall("postInstallationActionList/runProgram")
    install_hook = next(
        hook for hook in hooks if " install " in (hook.findtext("programArguments") or "")
    )
    arguments = install_hook.findtext("programArguments") or ""
    assert '--comfyui "${comfyui_directory}"' in arguments
    assert '--sdk "${installdir}/sdk"' in arguments
    assert '--state "${installdir}/installation.json"' in arguments
    assert install_hook.findtext("abortOnError") == "1"
    assert install_hook.findtext("program") == "${comfyui_python}"
    uninstaller = component.find("preUninstallationActionList/runProgram")
    assert uninstaller is not None
    assert 'uninstall --state "${installdir}/installation.json"' in (
        uninstaller.findtext("programArguments") or ""
    )
    assert uninstaller.findtext("abortOnError") == "1"
    assert uninstaller.findtext("program") == "${comfyui_python}"


def test_dependency_bundle_is_unpacked_before_sdk_validation() -> None:
    component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
    post_installation = component.find("postInstallationActionList")
    assert post_installation is not None
    actions = list(post_installation)
    unzip = next(action for action in actions if action.tag == "unzip")
    sdk_hook = next(action for action in actions if action.tag == "runProgram")
    assert actions.index(unzip) < actions.index(sdk_hook)
    assert unzip.findtext("destinationDirectory") == "${installdir}/sdk"
    assert unzip.findtext("addToUninstaller") == "1"
    assert (unzip.findtext("zipFile") or "").endswith(
        "deadline_cloud_for_comfyui_submitter-deps.zip"
    )


def test_selected_target_and_prefix_are_validated_before_payload_copy() -> None:
    component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
    ready = component.find("readyToInstallActionList")
    assert ready is not None
    unpack = ready.find("unpackFile")
    assert unpack is not None and unpack.get("folder") == "installation_helper"
    assert ready.findtext("runProgram/program") == "${comfyui_python}"
    assert ready.findtext("runProgram/programArguments") == (
        '-IBS install.py validate --comfyui "${comfyui_directory}" --prefix "${installdir}"'
    )
