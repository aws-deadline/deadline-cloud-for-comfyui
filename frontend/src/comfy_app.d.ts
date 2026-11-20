// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Types for the frontend app module ComfyUI supplies at /scripts/app.js.

declare module "*/scripts/app.js" {
  export interface ToastMessage {
    severity: "success" | "info" | "warn" | "error";
    summary: string;
    detail: string;
    life?: number;
  }

  export interface SidebarTab {
    id: string;
    icon: string;
    title: string;
    tooltip: string;
    type: "custom";
    render: (element: HTMLElement) => void;
    destroy?: () => void;
  }

  export interface ExtensionManager {
    registerSidebarTab(tab: SidebarTab): void;
    toast: { add(message: ToastMessage): void };
    workflow?: { activeWorkflow?: { filename?: string } | null };
    sidebarTab?: { activeSidebarTabId?: string | null };
    command: { execute(id: string): void };
  }

  export interface Command {
    id: string;
    label: string;
    icon: string;
    function: () => void;
  }

  export interface ComfyExtension {
    name: string;
    commands?: Command[];
    menuCommands?: { path: string[]; commands: string[] }[];
    actionBarButtons?: { icon: string; label: string; tooltip: string; onClick: () => void }[];
    setup?(): void;
    afterConfigureGraph?(): void;
  }

  // graphToPrompt returns the workflow in API format as output, and in UI format as workflow.
  export interface GraphPrompt {
    output: Record<string, unknown>;
    workflow: Record<string, unknown>;
  }

  export interface ComfyApp {
    extensionManager: ExtensionManager;
    registerExtension(extension: ComfyExtension): void;
    graphToPrompt(): Promise<GraphPrompt>;
  }

  export const app: ComfyApp;
}
