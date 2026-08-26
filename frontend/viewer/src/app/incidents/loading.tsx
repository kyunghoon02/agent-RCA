import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export default function Loading() {
  return (
    <div className="flex flex-col gap-3">
      <Skeleton className="h-8 w-48" />
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-14" />
        ))}
      </div>
      <Card className="p-3">
        <Skeleton className="h-8 w-full" />
      </Card>
      <Card className="p-3">
        <Skeleton className="h-64 w-full" />
      </Card>
    </div>
  );
}
