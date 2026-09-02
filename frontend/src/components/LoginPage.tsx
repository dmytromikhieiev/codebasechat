import { loginUrl } from "../api";

export function LoginPage() {
  return (
    <div className="flex h-screen flex-col items-center justify-center gap-4 bg-gray-50">
      <h1 className="text-2xl font-semibold text-gray-900">rag-codebase-chat</h1>
      <p className="text-gray-500">Chat with your GitHub codebase</p>
      <a
        href={loginUrl()}
        className="rounded-lg bg-gray-900 px-5 py-2.5 text-white transition hover:bg-gray-700"
      >
        Войти через GitHub
      </a>
    </div>
  );
}
