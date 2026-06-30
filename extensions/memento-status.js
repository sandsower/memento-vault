export function decorateStatusDetails(payload, bridgeDetails) {
	return {
		...payload,
		piBridge: {
			config: bridgeDetails?.config,
			configSources: bridgeDetails?.configSources ?? [],
			toolContextCount: bridgeDetails?.toolContextCount ?? 0,
			lifecycleCaptureQueued: Boolean(bridgeDetails?.lifecycleCaptureQueued),
			lastLifecycleReason: bridgeDetails?.lastLifecycleReason ?? "unknown",
		},
	};
}
