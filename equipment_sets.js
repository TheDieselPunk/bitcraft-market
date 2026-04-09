/**
 * equipment_sets.js — Shared equipment set persistence & stats calculation.
 *
 * Included on any page that needs to read or save equipment sets.
 * All state is kept in localStorage under 'equipSet_gathering' and 'equipSet_crafting'.
 *
 * Usage:
 *   EquipSets.saveSet('gathering', equippedItems, foodData, elixirData)
 *   EquipSets.loadSet('crafting')   → { equippedItems, foodData, elixirData, savedAt } | null
 *   EquipSets.getStats('gathering') → stats object | null
 *   EquipSets.clearSet('crafting')
 */
const EquipSets = (() => {

  // ── Stat ID constants (mirror of equipment_sim.html STAT) ──────────────────
  const STAT = {
    MAX_HEALTH:    'MaxHealth',
    MAX_STAMINA:   'MaxStamina',
    HP_REGEN:      'PassiveHealthRegenRate',
    STAM_REGEN:    'PassiveStaminaRegenRate',
    MOVE_MULT:     'MovementMultiplier',
    EVASION:       'Evasion',
    CRAFT_SPEED:   'CraftingSpeed',
    GATHER_SPEED:  'GatheringSpeed',
    BUILD_SPEED:   'BuildingSpeed',
  };

  const PROF_SPEED_STATS = {
    'CarpentrySpeed':      'Carpenter',
    'FarmingSpeed':        'Farmer',
    'FishingSpeed':        'Fisher',
    'ForagingSpeed':       'Forager',
    'ForestrySpeed':       'Forester',
    'HuntingSpeed':        'Hunter',
    'LeatherworkingSpeed': 'Leatherworker',
    'MasonrySpeed':        'Mason',
    'MiningSpeed':         'Miner',
    'ScholarSpeed':        'Scholar',
    'SmithingSpeed':       'Smith',
    'TailoringSpeed':      'Tailor',
  };

  const PROF_CRIT_STATS = {
    'CarpentryCritChance':      'Carpenter',
    'FarmingCritChance':        'Farmer',
    'FishingCritChance':        'Fisher',
    'ForagingCritChance':       'Forager',
    'ForestryCritChance':       'Forester',
    'HuntingCritChance':        'Hunter',
    'LeatherworkingCritChance': 'Leatherworker',
    'MasonryCritChance':        'Mason',
    'MiningCritChance':         'Miner',
    'ScholarCritChance':        'Scholar',
    'SmithingCritChance':       'Smith',
    'TailoringCritChance':      'Tailor',
  };

  // ── Stat accumulation (no DOM access) ─────────────────────────────────────

  function _accumStat(stats, s) {
    switch (s.id) {
      case STAT.MAX_HEALTH:    stats.maxHealth   += s.value; break;
      case STAT.MAX_STAMINA:   stats.maxStamina  += s.value; break;
      case STAT.HP_REGEN:      stats.hpRegen     += s.value; break;
      case STAT.STAM_REGEN:    stats.stamRegen   += s.value; break;
      case STAT.MOVE_MULT:     stats.moveMult    += s.value; break;
      case STAT.EVASION:       stats.evasion     += s.value; break;
      case STAT.GATHER_SPEED:  stats.gatherSpeed += s.value; break;
      case STAT.CRAFT_SPEED:   stats.craftSpeed  += s.value; break;
      case STAT.BUILD_SPEED:   stats.buildSpeed  += s.value; break;
      default: {
        const profSpeed = PROF_SPEED_STATS[s.id];
        if (profSpeed) {
          if (!stats.profBonuses[profSpeed]) stats.profBonuses[profSpeed] = { speed: 0, critChance: 0 };
          stats.profBonuses[profSpeed].speed += s.value;
          break;
        }
        const profCrit = PROF_CRIT_STATS[s.id];
        if (profCrit) {
          if (!stats.profBonuses[profCrit]) stats.profBonuses[profCrit] = { speed: 0, critChance: 0 };
          stats.profBonuses[profCrit].critChance += s.value;
        }
      }
    }
  }

  /**
   * Compute stats from serialized set data (no DOM access required).
   * @param {Object} equippedItems  slot → item entry (nulls are skipped)
   * @param {Object|null} foodData  meal object with buffs[]
   * @param {Object|null} elixirData elixir object with buffs[]
   * @returns {Object} stats
   */
  function computeStatsFromData(equippedItems, foodData, elixirData) {
    const stats = {
      maxHealth: 0, maxStamina: 0, hpRegen: 0, stamRegen: 0,
      moveMult: 0, evasion: 0, gatherSpeed: 0, craftSpeed: 0, buildSpeed: 0,
      profBonuses: {},
    };

    for (const item of Object.values(equippedItems || {})) {
      if (!item) continue;
      for (const s of (item.stats || [])) _accumStat(stats, s);
    }

    if (foodData) {
      for (const buff of foodData.buffs || [])
        for (const s of buff.stats || []) _accumStat(stats, s);
    }

    if (elixirData) {
      for (const buff of elixirData.buffs || [])
        for (const s of buff.stats || []) _accumStat(stats, s);
    }

    return stats;
  }

  // ── Persistence ────────────────────────────────────────────────────────────

  const STORAGE_KEYS = {
    gathering: 'equipSet_gathering',
    crafting:  'equipSet_crafting',
  };

  /**
   * Save an equipment set to localStorage.
   * @param {'gathering'|'crafting'} type
   * @param {Object} equippedItems  slot → item entry
   * @param {Object|null} foodData
   * @param {Object|null} elixirData
   */
  function saveSet(type, equippedItems, foodData, elixirData) {
    const key = STORAGE_KEYS[type];
    if (!key) return;
    localStorage.setItem(key, JSON.stringify({
      equippedItems: equippedItems || {},
      foodData:      foodData      || null,
      elixirData:    elixirData    || null,
      savedAt:       new Date().toISOString(),
    }));
  }

  /**
   * Load a saved equipment set. Returns null if nothing is saved.
   * @param {'gathering'|'crafting'} type
   * @returns {{equippedItems, foodData, elixirData, savedAt}|null}
   */
  function loadSet(type) {
    const key = STORAGE_KEYS[type];
    if (!key) return null;
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  /**
   * Clear a saved equipment set.
   * @param {'gathering'|'crafting'} type
   */
  function clearSet(type) {
    const key = STORAGE_KEYS[type];
    if (key) localStorage.removeItem(key);
  }

  /**
   * Get computed stats for a saved set, or null if not saved.
   * @param {'gathering'|'crafting'} type
   * @returns {Object|null}
   */
  function getStats(type) {
    const set = loadSet(type);
    if (!set) return null;
    return computeStatsFromData(set.equippedItems, set.foodData, set.elixirData);
  }

  // ── Formatting helpers ─────────────────────────────────────────────────────

  /**
   * Human-readable summary of speed bonuses from a stats object.
   * Returns e.g. "+15% Gather, +10% Craft" or "No speed bonuses"
   */
  function statsSummary(stats) {
    if (!stats) return 'No stats';
    const parts = [];
    if (stats.gatherSpeed) parts.push(`+${(stats.gatherSpeed * 100).toFixed(0)}% Gather`);
    if (stats.craftSpeed)  parts.push(`+${(stats.craftSpeed  * 100).toFixed(0)}% Craft`);
    for (const [prof, b] of Object.entries(stats.profBonuses || {})) {
      if (b.speed) parts.push(`+${(b.speed * 100).toFixed(0)}% ${prof}`);
    }
    return parts.length ? parts.join(', ') : 'No speed bonuses';
  }

  /**
   * Format a savedAt ISO timestamp as a relative "X ago" string.
   */
  function timeAgo(isoStr) {
    if (!isoStr) return '';
    const diff = Date.now() - new Date(isoStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1)  return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24)  return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  return { saveSet, loadSet, clearSet, getStats, computeStatsFromData, statsSummary, timeAgo };

})();
