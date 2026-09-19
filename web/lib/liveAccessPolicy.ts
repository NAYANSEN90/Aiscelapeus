const MIN_ACCESS_CODE_LENGTH = 16;

export interface LiveAccessPolicy {
  required: boolean;
  responderCode: string | null;
  clinicianCode: string | null;
  problems: string[];
}

/** One fail-closed policy shared by readiness and token minting. */
export function liveAccessPolicy(env: NodeJS.ProcessEnv): LiveAccessPolicy {
  const responderCode = env.RESPONDER_ACCESS_CODE?.trim() ?? "";
  const clinicianCode = env.CLINICIAN_ACCESS_CODE?.trim() ?? "";
  const required =
    env.NODE_ENV === "production" || responderCode.length > 0 || clinicianCode.length > 0;
  const problems: string[] = [];

  if (required) {
    if (responderCode.length < MIN_ACCESS_CODE_LENGTH) {
      problems.push("RESPONDER_ACCESS_CODE");
    }
    if (clinicianCode.length < MIN_ACCESS_CODE_LENGTH) {
      problems.push("CLINICIAN_ACCESS_CODE");
    }
    if (responderCode && clinicianCode && responderCode === clinicianCode) {
      problems.push("ACCESS_CODES_MUST_DIFFER");
    }
  }

  return {
    required,
    responderCode: responderCode || null,
    clinicianCode: clinicianCode || null,
    problems,
  };
}
