const INCIDENT_ID = /^inc-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const PARTICIPANT_ID = /^[A-Za-z0-9_-]{1,64}$/;

export function newIncidentId(
  randomUUID: () => string = () => crypto.randomUUID(),
): string {
  const incidentId = `inc-${randomUUID()}`;
  if (!INCIDENT_ID.test(incidentId)) {
    throw new Error("Secure UUID generator returned an invalid incident ID");
  }
  return incidentId;
}

export function responderIdentityFor(incidentId: string): string {
  if (!INCIDENT_ID.test(incidentId)) {
    throw new Error("A valid incident ID is required for responder identity");
  }
  const identity = `responder-${incidentId}`;
  if (!PARTICIPANT_ID.test(identity)) {
    throw new Error("Responder identity exceeds the LiveKit boundary");
  }
  return identity;
}
