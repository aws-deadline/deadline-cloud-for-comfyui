// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Stands in for ComfyUI's scripts/app.js. The graph has one LoadImage node whose image the
// harness changes, as an artist editing the workflow would. Commands run are recorded.
export const app = {
  image: "old.png",
  toasts: [],
  commands: [],
  graph: { extra: {} },
  async graphToPrompt() {
    const image = app.image;
    return {
      output: { 1: { class_type: "LoadImage", inputs: { image } } },
      workflow: { nodes: [{ id: 1, type: "LoadImage", widgets_values: [image] }] },
    };
  },
  extensionManager: {
    workflow: { activeWorkflow: { filename: "fox.json" } },
    toast: { add: (toast) => app.toasts.push(toast) },
    command: { execute: (id) => app.commands.push(id) },
  },
};
