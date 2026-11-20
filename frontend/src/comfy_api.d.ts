// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Types for the frontend API module ComfyUI supplies at /scripts/api.js.

declare module "*/scripts/api.js" {
  export interface ComfyApi {
    // This browser tab's websocket client ID, which the server can send events to.
    clientId?: string;
    fetchApi(path: string, init?: RequestInit): Promise<Response>;
    // Server events, such as the deadline.* ones the extension's routes send, arrive as CustomEvents.
    addEventListener<T = unknown>(type: string, listener: (event: CustomEvent<T>) => void): void;
  }

  export const api: ComfyApi;
}
