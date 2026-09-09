export interface Me {
  id: string;
  email: string;
  github_login: string | null;
}

export type RepoStatus = "pending_first_index" | "indexing" | "ready" | "failed";

export interface Repo {
  id: string;
  repo_full_name: string;
  default_branch: string;
  status: RepoStatus;
  last_indexed_sha: string | null;
  indexed_at: string | null;
  created_at: string;
}

export interface RepoDetail extends Repo {
  progress: number | null;
  files_done: number | null;
  files_total: number | null;
  error: string | null;
}

export interface AvailableRepo {
  installation_id: number;
  repo_full_name: string;
  default_branch: string;
}

export interface Source {
  file_path: string;
  start_line: number;
  end_line: number;
  function_name: string | null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  queryId?: string;
  feedback?: 1 | -1;
}
