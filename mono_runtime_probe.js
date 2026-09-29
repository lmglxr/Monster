// Read-only Mono runtime probe. It observes generated protobuf setters and never
// writes game memory or changes method return values.
const bossMonsterId = __BOSS_MONSTER_ID__;
const mono = Process.getModuleByName("mono-2.0-bdwgc.dll");

function monoFn(name, ret, args) {
  return new NativeFunction(mono.getExportByName(name), ret, args);
}

const monoGetRootDomain = monoFn("mono_get_root_domain", "pointer", []);
const monoThreadAttach = monoFn("mono_thread_attach", "pointer", ["pointer"]);
const monoAssemblyForeach = monoFn("mono_assembly_foreach", "void", ["pointer", "pointer"]);
const monoAssemblyGetImage = monoFn("mono_assembly_get_image", "pointer", ["pointer"]);
const monoImageGetName = monoFn("mono_image_get_name", "pointer", ["pointer"]);
const monoClassFromName = monoFn(
  "mono_class_from_name",
  "pointer",
  ["pointer", "pointer", "pointer"]
);
const monoClassGetMethodFromName = monoFn(
  "mono_class_get_method_from_name",
  "pointer",
  ["pointer", "pointer", "int"]
);
const monoCompileMethod = monoFn("mono_compile_method", "pointer", ["pointer"]);
const monoClassGetFieldFromName = monoFn(
  "mono_class_get_field_from_name",
  "pointer",
  ["pointer", "pointer"]
);
const monoClassGetParent = monoFn(
  "mono_class_get_parent",
  "pointer",
  ["pointer"]
);
const monoClassGetFields = monoFn(
  "mono_class_get_fields",
  "pointer",
  ["pointer", "pointer"]
);
const monoFieldGetName = monoFn("mono_field_get_name", "pointer", ["pointer"]);
const monoClassGetMethods = monoFn(
  "mono_class_get_methods",
  "pointer",
  ["pointer", "pointer"]
);
const monoMethodGetName = monoFn("mono_method_get_name", "pointer", ["pointer"]);
const monoFieldGetValue = monoFn(
  "mono_field_get_value",
  "void",
  ["pointer", "pointer", "pointer"]
);
const monoClassGetNestedTypes = monoFn(
  "mono_class_get_nested_types",
  "pointer",
  ["pointer", "pointer"]
);
const monoClassGetName = monoFn("mono_class_get_name", "pointer", ["pointer"]);

monoThreadAttach(monoGetRootDomain());

let gameImage = ptr(0);
const assemblyVisitor = new NativeCallback(function (assembly, unused) {
  const image = monoAssemblyGetImage(assembly);
  if (image.isNull()) return;
  const namePtr = monoImageGetName(image);
  if (!namePtr.isNull() && namePtr.readUtf8String() === "Assembly-CSharp") {
    gameImage = image;
  }
}, "void", ["pointer", "pointer"]);
monoAssemblyForeach(assemblyVisitor, ptr(0));

if (gameImage.isNull()) {
  send({ type: "probe_error", error: "Assembly-CSharp image not found" });
  throw new Error("Assembly-CSharp image not found");
}

const utf8Keepalive = [];
function utf8(value) {
  const p = Memory.allocUtf8String(value);
  utf8Keepalive.push(p);
  return p;
}

const objects = new Map();
const bossEntityIds = new Set();
let bossMonsterObject = null;
let hookCount = 0;

function stateFor(objectPointer) {
  const key = objectPointer.toString();
  let state = objects.get(key);
  if (state === undefined) {
    state = {};
    objects.set(key, state);
    if (objects.size > 4096) objects.clear();
  }
  return state;
}

function emitBossBaseInfo(state) {
  if (state.monsterId !== bossMonsterId) return;
  if (state.entityId !== undefined) bossEntityIds.add(state.entityId);
  send({
    type: "boss_seen",
    source: "SceneEntityBaseInfoSyncEvent",
    monster_id: state.monsterId,
    entity_id: state.entityId,
    hp: state.hp
  });
  if (state.hp !== undefined && state.hp <= 0) {
    send({
      type: "boss_dead",
      source: "SceneEntityBaseInfoSyncEvent.HP",
      monster_id: state.monsterId,
      entity_id: state.entityId,
      hp: state.hp
    });
  }
}

function findClass(namespaceName, className) {
  const klass = monoClassFromName(gameImage, utf8(namespaceName), utf8(className));
  if (klass.isNull()) throw new Error(`${namespaceName}.${className} not found`);
  return klass;
}

function findMethod(namespaceName, className, methodName, parameterCount) {
  const klass = findClass(namespaceName, className);
  const method = monoClassGetMethodFromName(
    klass,
    utf8(methodName),
    parameterCount
  );
  if (method.isNull()) {
    throw new Error(`${namespaceName}.${className}.${methodName} not found`);
  }
  const compiled = monoCompileMethod(method);
  if (compiled.isNull()) throw new Error(`${className}.${methodName} compile failed`);
  return compiled;
}

function hookSetter(className, methodName, callback) {
  const address = findMethod("Protoc", className, methodName, 1);
  Interceptor.attach(address, {
    onEnter(args) {
      try {
        callback(args[0], args[1].toInt32());
      } catch (error) {
        send({ type: "probe_warning", error: `${className}.${methodName}: ${error}` });
      }
    }
  });
  hookCount += 1;
}

// SceneEntityBaseInfoSyncEvent payload: correlate the instance entity id,
// monster configuration id and current HP on the same protobuf object.
hookSetter("EntityBaseInfo", "set_EntityId", (objectPointer, value) => {
  const state = stateFor(objectPointer);
  state.entityId = value;
  emitBossBaseInfo(state);
});
hookSetter("EntityBaseInfo", "set_MonsterId", (objectPointer, value) => {
  const state = stateFor(objectPointer);
  state.monsterId = value;
  emitBossBaseInfo(state);
});
hookSetter("EntityBaseInfo", "set_CurHp", (objectPointer, value) => {
  const state = stateFor(objectPointer);
  state.hp = value;
  emitBossBaseInfo(state);
});

// KillInfoSyncEvent payload contains KillEvent entries. MonsterId is enough to
// identify the configured boss; KilledId is retained for diagnostics.
hookSetter("KillEvent", "set_KilledId", (objectPointer, value) => {
  const state = stateFor(objectPointer);
  state.killedId = value;
  if (state.monsterId === bossMonsterId) {
    send({ type: "boss_dead", source: "KillInfoSyncEvent", monster_id: state.monsterId, entity_id: value });
  }
});
hookSetter("KillEvent", "set_MonsterId", (objectPointer, value) => {
  const state = stateFor(objectPointer);
  state.monsterId = value;
  if (value === bossMonsterId) {
    send({ type: "boss_dead", source: "KillInfoSyncEvent", monster_id: value, entity_id: state.killedId });
  }
});

// LifeDead is enum value 2 in the current assembly. Only accept it for an
// entity previously associated with the configured boss.
hookSetter("HpChangeInnerSync", "set_EntityId", (objectPointer, value) => {
  const state = stateFor(objectPointer);
  state.entityId = value;
  if (state.lifeState === 2 && bossEntityIds.has(value)) {
    send({ type: "boss_dead", source: "HpChangeSyncEvent.LifeDead", entity_id: value });
  }
});
hookSetter("HpChangeInnerSync", "set_LifeState", (objectPointer, value) => {
  const state = stateFor(objectPointer);
  state.lifeState = value;
  if (value === 2 && state.entityId !== undefined && bossEntityIds.has(state.entityId)) {
    send({ type: "boss_dead", source: "HpChangeSyncEvent.LifeDead", entity_id: state.entityId });
  }
});

hookSetter("HuntFatigueSync", "set_HuntFatigue", (objectPointer, value) => {
  send({ type: "fatigue", source: "HuntFatigueSyncEvent", value: value });
});

// The generated protobuf parser may assign backing fields directly and bypass
// property setters. Monster.OnDead is the authoritative gameplay transition,
// so observe it as the final runtime-level death source.
const monsterClass = findClass("CreatureCurios", "Monster");
const monsterIdField = monoClassGetFieldFromName(monsterClass, utf8("m_monsterId"));
if (monsterIdField.isNull()) throw new Error("CreatureCurios.Monster.m_monsterId not found");

// HP is declared on a base class in some builds. mono_class_get_field_from_name
// does not reliably return inherited fields, so walk the complete class chain.
function findFieldInHierarchy(klass, fieldName) {
  let current = klass;
  while (!current.isNull()) {
    const field = monoClassGetFieldFromName(current, utf8(fieldName));
    if (!field.isNull()) return field;
    current = monoClassGetParent(current);
  }
  return ptr(0);
}

function findMethodInHierarchy(klass, methodName, parameterCount) {
  let current = klass;
  while (!current.isNull()) {
    const method = monoClassGetMethodFromName(
      current,
      utf8(methodName),
      parameterCount
    );
    if (!method.isNull()) {
      const compiled = monoCompileMethod(method);
      if (!compiled.isNull()) return compiled;
    }
    current = monoClassGetParent(current);
  }
  throw new Error(`CreatureCurios.Monster.${methodName} not found in hierarchy`);
}

const monsterHpField = findFieldInHierarchy(monsterClass, "m_HP");
const monsterMaxHpField = findFieldInHierarchy(monsterClass, "m_MaxHP");

function collectMonsterHpCandidates(klass) {
  const candidates = [];
  let current = klass;
  while (!current.isNull()) {
    const fieldIterator = Memory.alloc(Process.pointerSize);
    fieldIterator.writePointer(ptr(0));
    while (true) {
      const field = monoClassGetFields(current, fieldIterator);
      if (field.isNull()) break;
      const namePointer = monoFieldGetName(field);
      if (!namePointer.isNull()) {
        const name = namePointer.readUtf8String();
        if (/hp|health|血|cur|max/i.test(name)) candidates.push(`field:${name}`);
      }
    }
    const methodIterator = Memory.alloc(Process.pointerSize);
    methodIterator.writePointer(ptr(0));
    while (true) {
      const method = monoClassGetMethods(current, methodIterator);
      if (method.isNull()) break;
      const namePointer = monoMethodGetName(method);
      if (!namePointer.isNull()) {
        const name = namePointer.readUtf8String();
        if (/hp|health|血|cur|max/i.test(name)) candidates.push(`method:${name}`);
      }
    }
    current = monoClassGetParent(current);
  }
  return Array.from(new Set(candidates));
}

// Some builds expose HP only through methods, even though the assembly still
// contains the old field names in metadata. Prefer the methods when present.
let monsterGetHp = null;
let monsterGetMaxHp = null;
try {
  monsterGetHp = new NativeFunction(
    findMethodInHierarchy(monsterClass, "GetCurHp", -1),
    "int",
    ["pointer"]
  );
} catch (error) {
  send({ type: "probe_diagnostic", get_cur_hp_error: String(error) });
}
try {
  monsterGetMaxHp = new NativeFunction(
    findMethodInHierarchy(monsterClass, "GetMaxHp", -1),
    "int",
    ["pointer"]
  );
} catch (error) {
  send({ type: "probe_diagnostic", get_max_hp_error: String(error) });
}

send({
  type: "probe_diagnostic",
  monster_hp_field_found: !monsterHpField.isNull(),
  monster_max_hp_field_found: !monsterMaxHpField.isNull(),
  get_cur_hp_found: monsterGetHp !== null,
  get_max_hp_found: monsterGetMaxHp !== null,
  monster_hp_candidates: collectMonsterHpCandidates(monsterClass)
});

function readIntField(objectPointer, field) {
  if (field.isNull()) return undefined;
  const valueBuffer = Memory.alloc(4);
  monoFieldGetValue(objectPointer, field, valueBuffer);
  return valueBuffer.readS32();
}

function emitBossRuntimeHp(objectPointer, source) {
  try {
    const monsterId = readIntField(objectPointer, monsterIdField);
    if (monsterId !== bossMonsterId) return;
    const hp = monsterGetHp !== null
      ? monsterGetHp(objectPointer)
      : readIntField(objectPointer, monsterHpField);
    const maxHp = monsterGetMaxHp !== null
      ? monsterGetMaxHp(objectPointer)
      : readIntField(objectPointer, monsterMaxHpField);
    if (hp === undefined && maxHp === undefined) return;
    send({
      type: "boss_hp",
      source: source,
      monster_id: monsterId,
      hp: hp,
      max_hp: maxHp
    });
    if (hp !== undefined && hp <= 0) {
      send({
        type: "boss_dead",
        source: "Monster.GetCurHp",
        monster_id: monsterId,
        hp: hp
      });
    }
  } catch (error) {
    // The managed object may already have been destroyed after leaving a
    // dungeon. Stop polling this stale pointer until the next OnShow event.
    bossMonsterObject = null;
    send({ type: "probe_warning", error: `Monster HP read: ${error}` });
  }
}

const monsterOnDead = findMethod("CreatureCurios", "Monster", "OnDead", -1);
Interceptor.attach(monsterOnDead, {
  onEnter(args) {
    try {
      const valueBuffer = Memory.alloc(4);
      monoFieldGetValue(args[0], monsterIdField, valueBuffer);
      const monsterId = valueBuffer.readS32();
      send({ type: "monster_dead_observed", monster_id: monsterId });
      if (monsterId === bossMonsterId) {
        send({
          type: "boss_dead",
          source: "Monster.OnDead",
          monster_id: monsterId
        });
      }
    } catch (error) {
      send({ type: "probe_warning", error: `Monster.OnDead: ${error}` });
    }
  }
});
hookCount += 1;

const monsterOnShow = findMethod("CreatureCurios", "Monster", "OnShow", -1);
Interceptor.attach(monsterOnShow, {
  onLeave(retval) {
    try {
      const valueBuffer = Memory.alloc(4);
      // `this` is not available onLeave unless retained from onEnter.
      if (this.monsterObject === undefined) return;
      monoFieldGetValue(this.monsterObject, monsterIdField, valueBuffer);
      const monsterId = valueBuffer.readS32();
      if (monsterId === bossMonsterId) {
        bossMonsterObject = this.monsterObject;
        send({
          type: "boss_seen",
          source: "Monster.OnShow",
          monster_id: monsterId
        });
        emitBossRuntimeHp(this.monsterObject, "Monster.OnShow.fields");
      }
    } catch (error) {
      send({ type: "probe_warning", error: `Monster.OnShow: ${error}` });
    }
  },
  onEnter(args) {
    this.monsterObject = args[0];
  }
});
hookCount += 1;

// The current game build initializes m_HP/m_MaxHP after OnShow. Poll the
// managed fields briefly and emit changes so the Python side can classify the
// boss even when the protobuf CurHp setter is bypassed.
setInterval(() => {
  if (bossMonsterObject !== null) {
    emitBossRuntimeHp(bossMonsterObject, "Monster.fields.poll");
  }
}, 100);

// MainHUDForm starts a compiler-generated coroutine with the exact fatigue
// target. Reading its managed field avoids OCR and does not depend on protobuf
// property setters (generated parsers may write backing fields directly).
let fatigueCoroutineClass = ptr(0);
const mainHudClass = findClass("CreatureCurios", "MainHUDForm");
const nestedIterator = Memory.alloc(Process.pointerSize);
nestedIterator.writePointer(ptr(0));
while (true) {
  const nested = monoClassGetNestedTypes(mainHudClass, nestedIterator);
  if (nested.isNull()) break;
  const nestedName = monoClassGetName(nested).readUtf8String();
  if (nestedName.indexOf("AnimateHuntFatigue") !== -1) {
    fatigueCoroutineClass = nested;
    break;
  }
}
if (!fatigueCoroutineClass.isNull()) {
  const fatigueField = monoClassGetFieldFromName(
    fatigueCoroutineClass,
    utf8("huntFatigue")
  );
  const moveNextMethod = monoClassGetMethodFromName(
    fatigueCoroutineClass,
    utf8("MoveNext"),
    -1
  );
  if (!fatigueField.isNull() && !moveNextMethod.isNull()) {
    const moveNextAddress = monoCompileMethod(moveNextMethod);
    let lastRuntimeFatigue = null;
    Interceptor.attach(moveNextAddress, {
      onEnter(args) {
        try {
          const valueBuffer = Memory.alloc(4);
          monoFieldGetValue(args[0], fatigueField, valueBuffer);
          const value = Math.round(valueBuffer.readFloat());
          if (
            Number.isFinite(value) &&
            value >= -1000 &&
            value <= 1000 &&
            value !== lastRuntimeFatigue
          ) {
            lastRuntimeFatigue = value;
            send({
              type: "fatigue",
              source: "HuntFatigueSyncEvent.Animation",
              value: value
            });
          }
        } catch (error) {
          send({ type: "probe_warning", error: `fatigue coroutine: ${error}` });
        }
      }
    });
    hookCount += 1;
  }
}

send({ type: "probe_ready", hooks: hookCount, boss_monster_id: bossMonsterId });
