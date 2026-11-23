# preflight\_report\.json

\{

"simulation\_software\_checks\_passed": true,

"real\_machine\_motion\_enabled": false,

"ready\_for\_real\_machine\_commissioning": false,

"missing\_enterprise\_evidence": \[

"real\_robot\.vendor\_model",

"real\_robot\.serial\_number",

"real\_robot\.urdf\_and\_meshes",

"real\_robot\.vendor\_driver\_and\_controller",

"real\_robot\.vendor\_joint\_limits",

"real\_robot\.calibration\_file",

"tool\.measured\_tcp\_transform",

"tool\.mass\_and\_center\_of\_gravity",

"tool\.collision\_cad",

"tool\.wheel\_geometry\_and\_wear",

"cell\.fixture\_and\_environment\_cad",

"cell\.measured\_base\_transform",

"cell\.camera\_intrinsics",

"cell\.hand\_eye\_transform\_and\_error",

"cell\.actual\_workpiece\_scan\_or\_cad",

"commissioning\.offline\_alignment\_verified",

"commissioning\.controller\_dry\_run\_verified",

"commissioning\.workpiece\_clearance\_verified",

"commissioning\.force\_sensor\_calibration",

"commissioning\.validated\_process\_parameters"

\],

"reason": "simulation\-only build; placeholders are not real\-machine qualification",

"current\_motion\_mode": "isolated joint\-state replay; no controller execution",

"required\_gate\_sequence": \[

"model\_and\_limits",

"hand\_eye\_calibration",

"controller\_dry\_run",

"low\_speed\_air\_run",

"clearance\_verification",

"force\_control\_calibration",

"low\_energy\_trial"

\]

\}

