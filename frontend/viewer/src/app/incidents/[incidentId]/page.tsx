import { IncidentDetailView } from "@/components/incident-detail/incident-detail-view";

export const metadata = { title: "Incident · Agent RCA" };

export default async function IncidentDetailPage({
  params,
}: {
  params: Promise<{ incidentId: string }>;
}) {
  const { incidentId } = await params;
  return <IncidentDetailView incidentId={incidentId} />;
}
