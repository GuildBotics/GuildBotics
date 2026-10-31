export function environmentValue(environment, name) {
  const key = Object.keys(environment).find(
    (candidate) => candidate.toUpperCase() === name.toUpperCase(),
  );
  return key ? environment[key] : undefined;
}

export function withEnvironment(environment, overrides) {
  const result = { ...environment };
  const names = new Set(Object.keys(overrides).map((name) => name.toUpperCase()));
  for (const key of Object.keys(result)) {
    if (names.has(key.toUpperCase())) {
      delete result[key];
    }
  }
  return { ...result, ...overrides };
}

export function withoutEnvironment(environment, names) {
  const omitted = new Set(names.map((name) => name.toUpperCase()));
  return Object.fromEntries(
    Object.entries(environment).filter(([key]) => !omitted.has(key.toUpperCase())),
  );
}
