import type { AvailableRepo, Me, Repo, RepoDetail } from "./types";

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ error: "unknown", message: response.statusText }));
    throw new ApiError(response.status, body.error ?? "unknown", body.message ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

export function loginUrl(): string {
  return `${API_BASE_URL}/api/v1/auth/login`;
}

export function installUrl(): string {
  return `${API_BASE_URL}/api/v1/github/install`;
}

export function getMe(): Promise<Me> {
  return request<Me>("/api/v1/auth/me");
}

export function logout(): Promise<{ status: string }> {
  return request("/api/v1/auth/logout", { method: "POST" });
}

export function listRepos(): Promise<Repo[]> {
  return request<Repo[]>("/api/v1/repos");
}

export function getRepo(repoId: string): Promise<RepoDetail> {
  return request<RepoDetail>(`/api/v1/repos/${repoId}`);
}

export function reindexRepo(repoId: string): Promise<{ status: string }> {
  return request(`/api/v1/repos/${repoId}/reindex`, { method: "POST" });
}

export function syncInstallations(repoFullName?: string): Promise<{ synced_repos: string[] }> {
  return request("/api/v1/github/sync", {
    method: "POST",
    body: JSON.stringify(repoFullName ? { repo_full_name: repoFullName } : {}),
  });
}

export function listAvailableRepos(): Promise<AvailableRepo[]> {
  return request<AvailableRepo[]>("/api/v1/github/available-repos");
}

export function sendFeedback(queryId: string, rating: 1 | -1, comment?: string): Promise<{ id: string }> {
  return request(`/api/v1/queries/${queryId}/feedback`, {
    method: "POST",
    body: JSON.stringify({ rating, comment }),
  });
}
