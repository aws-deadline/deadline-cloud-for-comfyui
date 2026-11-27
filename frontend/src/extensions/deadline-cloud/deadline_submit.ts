// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import { app } from "../../scripts/app.js";
import {
  SIDEBAR_TAB_ID,
  injectStyles,
  listenForSubmissions,
  openPanel,
  openPopover,
  renderPanel,
  togglePopover,
  workflowLoaded,
} from "./deadline_ui.js";

const SUBMIT_COMMAND_ID = "DeadlineCloud.Submit";
const PANEL_COMMAND_ID = "DeadlineCloud.OpenPanel";
// The deadline-cloud library's logo, which deadline_ui.ts styles.
const ICON = "dl-logo";

let destroyPanel: (() => void) | null = null;
let panelElement: HTMLElement | null = null;

app.registerExtension({
  name: "DeadlineCloud.Submitter",
  commands: [
    {
      id: SUBMIT_COMMAND_ID,
      label: "Submit to Deadline Cloud",
      icon: ICON,
      function: openPopover,
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
      label: "Submit to Deadline Cloud",
      tooltip: "Submit this workflow to Deadline Cloud",
      class: "dl-action-button",
      onClick: togglePopover,
    },
  ],
  setup() {
    // The action bar button and the sidebar tab show before the panel opens.
    injectStyles();
    app.extensionManager.registerSidebarTab({
      id: SIDEBAR_TAB_ID,
      icon: ICON,
      title: "Deadline Cloud",
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
