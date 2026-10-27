function objectOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function normalizeComponentPayload(payload: unknown): Record<string, unknown> {
  const entity = objectOrEmpty(payload);
  const queryResponse = objectOrEmpty(objectOrEmpty(entity.query).response);
  if (Object.keys(queryResponse).length) {
    return queryResponse;
  }
  const directQuery = objectOrEmpty(entity.query);
  if (Object.keys(directQuery).length) {
    return directQuery;
  }
  const actionResponse = objectOrEmpty(objectOrEmpty(entity.action).response);
  if (Object.keys(actionResponse).length) {
    return actionResponse;
  }
  const directAction = objectOrEmpty(entity.action);
  if (Object.keys(directAction).length) {
    return directAction;
  }
  const nestedResponse = objectOrEmpty(objectOrEmpty(entity.result).response);
  if (Object.keys(nestedResponse).length) {
    return nestedResponse;
  }
  return entity;
}
