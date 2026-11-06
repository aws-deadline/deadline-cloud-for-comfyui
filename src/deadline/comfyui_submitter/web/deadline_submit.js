// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import { app } from "../../scripts/app.js";
import {
  SIDEBAR_TAB_ID,
  listenForSubmissions,
  openDialog,
  openPanel,
  renderPanel,
  workflowLoaded,
} from "./deadline_ui.js";

const SUBMIT_COMMAND_ID = "DeadlineCloud.Submit";
const PANEL_COMMAND_ID = "DeadlineCloud.OpenPanel";
const ICON = "pi pi-cloud-upload";

let destroyPanel = null;
let panelElement = null;

app.registerExtension({
  name: "DeadlineCloud.Submitter",
  commands: [
    {
      id: SUBMIT_COMMAND_ID,
      label: "Submit to Deadline Cloud…",
      icon: ICON,
      function: openDialog,
    },
    {
      id: PANEL_COMMAND_ID,
      label: "Deadline Cloud Panel",
      icon: ICON,
      function: openPanel,
    },
  ],
  menuCommands: [
    { path: ["Workflow"], commands: [SUBMIT_COMMAND_ID, PANEL_COMMAND_ID] },
  ],
  actionBarButtons: [
    {
      icon: ICON,
      label: "Deadline",
      tooltip: "Submit to Deadline Cloud",
      onClick: openDialog,
    },
  ],
  setup() {
    app.extensionManager.registerSidebarTab({
      id: SIDEBAR_TAB_ID,
      icon: ICON,
      title: "Deadline",
      tooltip: "Deadline Cloud",
      type: "custom",
      // The frontend calls render from a Vue ref, again on each update of the tab's component.
      render: (element) => {
        if (element === panelElement) {
          return;
        }
        destroyPanel?.();
        panelElement = element;
        destroyPanel = renderPanel(element);
      },
      destroy: () => {
        destroyPanel?.();
        destroyPanel = null;
        panelElement = null;
      },
    });
    listenForSubmissions();
  },
  afterConfigureGraph() {
    workflowLoaded();
  },
});
