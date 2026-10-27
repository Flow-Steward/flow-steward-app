import { describe, expect, it } from 'vitest';

import { acyclicAdjacency, columnsFor } from './canvas-layout-ranking';

function graph(edges: readonly (readonly [string, readonly string[]])[]): ReadonlyMap<string, readonly string[]> {
  return new Map(edges);
}

function columns(entry: string, ids: readonly string[], adjacency: ReadonlyMap<string, readonly string[]>) {
  return Object.fromEntries(columnsFor(entry, ids, adjacency));
}

describe('acyclicAdjacency', () => {
  it('starts DFS at the entry before workflow roots and removes only active-path edges', () => {
    const adjacency = graph([
      ['root', ['B']],
      ['A', ['B']],
      ['B', ['A']],
    ]);

    expect([...acyclicAdjacency('A', ['root', 'B', 'A'], adjacency)]).toEqual([
      ['root', ['B']],
      ['B', []],
      ['A', ['B']],
    ]);
  });

  it('visits remaining roots before rootless workflow nodes', () => {
    const adjacency = graph([
      ['B', ['C']],
      ['C', ['B']],
      ['root', ['C']],
    ]);

    expect([...acyclicAdjacency('', ['B', 'C', 'root'], adjacency)]).toEqual([
      ['B', []],
      ['C', ['B']],
      ['root', ['C']],
    ]);
  });

  it('chooses the first workflow node for a rootless cycle', () => {
    const adjacency = graph([['C', ['A']], ['A', ['B']], ['B', ['C']]]);

    expect([...acyclicAdjacency('', ['B', 'C', 'A'], adjacency)]).toEqual([
      ['B', ['C']],
      ['C', ['A']],
      ['A', []],
    ]);
  });

  it('preserves forward and completed cross edges, duplicate edges, and target order', () => {
    const adjacency = graph([
      ['A', ['B', 'C', 'D', 'B']],
      ['B', ['D']],
      ['C', ['D', 'B']],
      ['D', ['A']],
    ]);

    expect([...acyclicAdjacency('A', ['A', 'B', 'C', 'D'], adjacency)]).toEqual([
      ['A', ['B', 'C', 'D', 'B']],
      ['B', ['D']],
      ['C', ['D', 'B']],
      ['D', []],
    ]);
  });

  it('removes self loops and ignores out-of-scope source and target ids', () => {
    const adjacency = graph([
      ['outside', ['A']],
      ['A', ['A', 'outside', 'B']],
      ['B', ['outside']],
    ]);

    expect([...acyclicAdjacency('outside', ['A', 'B', 'island'], adjacency)]).toEqual([
      ['A', ['B']],
      ['B', []],
      ['island', []],
    ]);
  });
});

describe('columnsFor', () => {
  it('preserves longest-path ranks in a complete convergent entry-reachable DAG', () => {
    const adjacency = graph([
      ['A', ['B', 'C', 'E']],
      ['B', ['D']],
      ['C', ['E']],
      ['D', ['E']],
      ['E', ['F']],
    ]);

    expect(columns('A', ['A', 'B', 'C', 'D', 'E', 'F'], adjacency)).toEqual({
      A: 0, B: 1, C: 1, D: 2, E: 3, F: 4,
    });
  });

  it('propagates a later longer path across completed cross edges', () => {
    const adjacency = graph([
      ['A', ['B', 'C']],
      ['B', ['D']],
      ['C', ['B']],
      ['D', ['E']],
    ]);

    expect(columns('A', ['A', 'B', 'C', 'D', 'E'], adjacency)).toEqual({
      A: 0, B: 2, C: 1, D: 3, E: 4,
    });
  });

  it('uses one common base for all disconnected pieces including a rootless cycle', () => {
    const adjacency = graph([
      ['A', ['B']],
      ['C', ['D']],
      ['F', ['G']],
      ['G', ['H']],
      ['H', ['F']],
    ]);

    expect(columns('A', ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'], adjacency)).toEqual({
      A: 0, B: 1, C: 2, D: 3, E: 2, F: 2, G: 3, H: 4,
    });
  });

  it.each(['', 'unknown'])('ranks chains from zero when entry is %j', (entry) => {
    const adjacency = graph([['A', ['B']], ['B', ['C']], ['D', ['E']]]);

    expect(columns(entry, ['C', 'B', 'A', 'E', 'D', 'island'], adjacency)).toEqual({
      A: 0, B: 1, C: 2, D: 0, E: 1, island: 0,
    });
  });

  it('uses longest paths from all disconnected roots sharing a descendant', () => {
    const adjacency = graph([['A', ['C']], ['B', ['D']], ['D', ['C']], ['C', ['E']]]);

    expect(columns('', ['A', 'B', 'C', 'D', 'E'], adjacency)).toEqual({
      A: 0, B: 0, C: 2, D: 1, E: 3,
    });
  });

  it('keeps already ranked main nodes fixed when a disconnected node points to them', () => {
    const adjacency = graph([['A', ['B']], ['C', ['D']], ['D', ['A', 'B']]]);

    expect(columns('A', ['A', 'B', 'C', 'D'], adjacency)).toEqual({ A: 0, B: 1, C: 2, D: 3 });
    expect(acyclicAdjacency('A', ['A', 'B', 'C', 'D'], adjacency).get('D')).toEqual(['A', 'B']);
  });

  it('terminates for a self cycle and keeps a single isolated entry at zero', () => {
    expect(columns('A', ['A'], graph([['A', ['A']]]))).toEqual({ A: 0 });
    expect(columns('', ['A'], graph([['A', ['A']]]))).toEqual({ A: 0 });
    expect(columns('A', ['A'], new Map())).toEqual({ A: 0 });
  });

  it('ignores out-of-scope shortcuts, incoming edges, and an out-of-scope entry', () => {
    const adjacency = graph([
      ['A', ['outside']],
      ['outside', ['B', 'C']],
      ['B', ['C']],
    ]);

    expect(columns('A', ['A', 'B', 'C'], adjacency)).toEqual({ A: 0, B: 1, C: 2 });
    expect(columns('outside', ['A', 'B', 'C'], adjacency)).toEqual({ A: 0, B: 0, C: 1 });
  });

  it('handles empty scopes safely', () => {
    expect([...acyclicAdjacency('A', [], graph([['A', ['A']]]))]).toEqual([]);
    expect([...columnsFor('A', [], graph([['A', ['A']]]))]).toEqual([]);
  });

  it('is repeatable and leaves workflow order, full adjacency and readonly arrays unchanged', () => {
    const ids = Object.freeze(['C', 'A', 'B', 'D']);
    const adjacency = graph([
      ['C', Object.freeze(['A', 'D'])],
      ['A', Object.freeze(['B', 'B'])],
      ['B', Object.freeze(['C'])],
      ['D', Object.freeze(['B'])],
    ]);
    const before = [...adjacency].map(([id, targets]) => [id, [...targets]]);
    const firstRankGraph = acyclicAdjacency('A', ids, adjacency);
    const firstColumns = columnsFor('A', ids, adjacency);

    expect(columns('A', ids, adjacency)).toEqual({ A: 0, B: 1, C: 2, D: 3 });
    expect([...acyclicAdjacency('A', ids, adjacency)]).toEqual([...firstRankGraph]);
    expect([...columnsFor('A', ids, adjacency)]).toEqual([...firstColumns]);
    expect(ids).toEqual(['C', 'A', 'B', 'D']);
    expect([...adjacency]).toEqual(before);
    firstRankGraph.get('A')?.push('changed-output');
    expect(adjacency.get('A')).toEqual(['B', 'B']);
  });

  it.each([false, true])('ranks a 201-node chain with a closing cycle = %s', (cycle) => {
    const ids = Array.from({ length: 201 }, (_, index) => `node-${index}`);
    const adjacency = graph(ids.map((id, index) => [
      id, index < ids.length - 1 ? [ids[index + 1]] : cycle ? [ids[0]] : [],
    ]));
    const expected = Object.fromEntries(ids.map((id, index) => [id, index]));

    expect(columns(ids[0], ids, adjacency)).toEqual(expected);
    expect(columns('', ids, adjacency)).toEqual(expected);
    expect(adjacency.get(ids[ids.length - 1])).toEqual(cycle ? [ids[0]] : []);
  });
});
