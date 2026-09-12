import { Sidebar } from "./Sidebar";

export function AppFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className="app-frame bee-workspace">
      <Sidebar />
      <main className="workspace">{children}</main>
    </div>
  );
}
