export async function runAndFinalizeLoadSoakEvidence({
  captureResourceRecovery,
  finishProductActivity,
  runCollaboration,
  writeResourceEvidence,
  writeScenarioEvidence,
}) {
  const required = {
    captureResourceRecovery,
    finishProductActivity,
    runCollaboration,
    writeResourceEvidence,
    writeScenarioEvidence,
  }
  for (const [name, callback] of Object.entries(required)) {
    if (typeof callback !== 'function') {
      throw new Error(`Load-soak finalization requires ${name}.`)
    }
  }

  const collaboration = await settle(runCollaboration)
  const productActivity = await settle(finishProductActivity)
  const resourceRecovery = await settle(captureResourceRecovery)
  const evidence = {
    axes: {
      collaboration: collaboration.status,
      productActivity: productActivity.status,
      resourceRecovery: resourceRecovery.status,
    },
    collaboration: fulfilledValue(collaboration),
    productActivity: fulfilledValue(productActivity),
    resources: fulfilledValue(resourceRecovery),
  }
  const scenarioInput = {
    axes: evidence.axes,
    collaboration: evidence.collaboration,
    supplemental: {
      commentsAndNavigationPassed: (
        evidence.productActivity?.commentsAndNavigationPassed === true
      ),
      featureActivityPassed: evidence.productActivity?.featureActivityPassed === true,
      identityMatrixPassed: evidence.collaboration?.connections === 25,
      resourceRecoveryPassed: evidence.resources?.recovery?.passed === true,
    },
  }
  const persistence = await Promise.allSettled([
    Promise.resolve().then(() => writeResourceEvidence(evidence)),
    Promise.resolve().then(() => writeScenarioEvidence(scenarioInput)),
  ])

  const failures = []
  collectRejected(failures, collaboration, 'collaboration load')
  collectRejected(failures, productActivity, 'product activity')
  collectRejected(failures, resourceRecovery, 'resource recovery')
  if (
    collaboration.status === 'fulfilled'
      && collaboration.value?.passed !== true
  ) {
    failures.push(new Error('Load-soak collaboration durability gates failed.'))
  }
  if (
    productActivity.status === 'fulfilled'
      && (
        productActivity.value?.commentsAndNavigationPassed !== true
        || productActivity.value?.featureActivityPassed !== true
      )
  ) {
    failures.push(new Error('Load-soak mixed product activity gates failed.'))
  }
  if (
    resourceRecovery.status === 'fulfilled'
      && resourceRecovery.value?.recovery?.passed !== true
  ) {
    failures.push(new Error('Load-soak resource recovery gate failed.'))
  }
  collectRejected(failures, persistence[0], 'resource evidence persistence')
  collectRejected(failures, persistence[1], 'scenario evidence persistence')
  if (failures.length > 0) {
    throw new AggregateError(
      failures,
      `Load-soak failed ${failures.length} independent completion gate(s).`,
    )
  }
  return evidence
}

async function settle(callback) {
  try {
    return { status: 'fulfilled', value: await callback() }
  } catch (reason) {
    return { reason, status: 'rejected' }
  }
}

function fulfilledValue(result) {
  return result.status === 'fulfilled' ? result.value : null
}

function collectRejected(failures, result, label) {
  if (result.status !== 'rejected') return
  failures.push(
    result.reason instanceof Error
      ? result.reason
      : new Error(`${label} failed: ${String(result.reason)}`),
  )
}
