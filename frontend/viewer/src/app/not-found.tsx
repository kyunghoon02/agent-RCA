import Link from "next/link";
import { FileQuestion } from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-2xl py-16">
      <EmptyState
        icon={FileQuestion}
        title="Page not found"
        description="This Viewer only serves the Incident list and Incident detail routes."
      >
        <Button asChild variant="outline" size="sm" className="mt-2">
          <Link href="/incidents">Back to Incidents</Link>
        </Button>
      </EmptyState>
    </div>
  );
}
