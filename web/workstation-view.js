import * as THREE from "three";
import { OrbitControls } from "/vendor/OrbitControls.js";

export class WorkstationView {
  constructor(element, onSelect) {
    this.element = element;
    this.onSelect = onSelect;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color("#dde5ec");
    this.camera = new THREE.PerspectiveCamera(43, 1, 0.02, 60);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(3.3, -4.4, 3.5);
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.5;
    element.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(-0.7, -0.45, 1.1);
    this.controls.enableDamping = !matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.controls.dampingFactor = 0.12;
    this.controls.minDistance = 0.25;
    this.controls.maxDistance = 15;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x6f8194, 2.2));
    const light = new THREE.DirectionalLight(0xffffff, 3);
    light.position.set(3, -2, 7);
    this.scene.add(light);
    const fill = new THREE.DirectionalLight(0xc9dcf0, 1.5);
    fill.position.set(-3, 4, 4);
    this.scene.add(fill);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(12, 12), new THREE.MeshStandardMaterial({ color: 0xcbd6df, roughness: 0.95 }));
    floor.position.z = -0.021;
    this.scene.add(floor);
    const grid = new THREE.GridHelper(10, 20, 0x8fa4b5, 0xb3c3cf);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = -0.019;
    this.scene.add(grid);
    this.root = new THREE.Group();
    this.scene.add(this.root);
    this.motionRoot = new THREE.Group();
    this.root.add(this.motionRoot);
    this.bodies = new Map();
    this.geometries = new Map();
    this.initialStock = new Map();
    this.objects = [];
    this.components = [];
    this.visibility = new Map();
    this.semantic = true;
    this.follow = false;
    this.lastTime = performance.now();
    this.raycaster = new THREE.Raycaster();
    this.selectionBox = new THREE.Box3Helper(new THREE.Box3(), 0x087763);
    this.selectionBox.visible = false;
    this.scene.add(this.selectionBox);
    new ResizeObserver(() => this.resize()).observe(element);
    this.renderer.domElement.addEventListener("pointerdown", (event) => { this.pointerStart = [event.clientX, event.clientY]; });
    this.renderer.domElement.addEventListener("pointerup", (event) => this.pick(event));
    element.addEventListener("keydown", (event) => {
      const actions = { ArrowLeft: () => this.orbit(-0.15), ArrowRight: () => this.orbit(0.15), ArrowUp: () => this.orbit(0, 0.12), ArrowDown: () => this.orbit(0, -0.12), "+": () => this.zoom(0.85), "=": () => this.zoom(0.85), "-": () => this.zoom(1.15) };
      if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
    });
    this.resize();
    this.animate();
  }

  resize() {
    const width = this.element.clientWidth, height = this.element.clientHeight;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / Math.max(1, height);
    this.camera.updateProjectionMatrix();
  }

  async load(base, signal) {
    const response = await fetch(base + "model.json", { signal });
    if (!response.ok) throw new Error("整站网格清单读取失败");
    const manifest = await response.json();
    const binaryResponse = await fetch(base + manifest.binary, { signal });
    if (!binaryResponse.ok) throw new Error("整站 CAD 网格读取失败");
    const binary = await binaryResponse.arrayBuffer();
    if (signal?.aborted) return;
    for (const object of this.objects) object.material.dispose();
    for (const geometry of this.geometries.values()) geometry.dispose();
    for (const root of [this.pathRoot, this.cloudRoot, this.motionRoot]) {
      for (const child of root?.children || []) { child.geometry?.dispose(); child.material?.dispose(); }
    }
    this.root.clear();
    this.motionRoot = new THREE.Group();
    this.root.add(this.motionRoot);
    this.objects = [];
    this.bodies.clear();
    this.geometries.clear();
    this.initialStock.clear();
    this.components = manifest.components;
    this.manifest = manifest;
    this.selected = null;
    this.selectionBox.visible = false;
    this.visibility = new Map(this.components.map((item) => [item.id, item.id !== "fences"]));
    for (const item of manifest.geometries) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(binary, item.vertex_offset, item.vertex_count * 3).slice(), 3));
      geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(binary, item.index_offset, item.index_count), 1));
      geometry.computeVertexNormals();
      geometry.computeBoundingSphere();
      this.geometries.set(item.id, geometry);
    }
    for (const item of manifest.meshes) {
      if (item.name === "st_floor_0") continue;
      if (!this.bodies.has(item.body)) {
        const body = new THREE.Group();
        this.root.add(body);
        this.bodies.set(item.body, body);
      }
      const component = this.components.find((part) => part.id === item.component);
      const natural = new THREE.Color(item.color[0], item.color[1], item.color[2]);
      const material = new THREE.MeshStandardMaterial({ color: this.semantic ? component.color : natural, roughness: 0.65, metalness: 0.15, side: THREE.DoubleSide });
      const mesh = new THREE.Mesh(this.geometries.get(item.geometry), material);
      mesh.position.fromArray(item.position);
      mesh.quaternion.set(item.quaternion[1], item.quaternion[2], item.quaternion[3], item.quaternion[0]);
      mesh.userData = { ...item, natural };
      if (item.component === "defects" && !this.initialStock.has(item.geometry)) {
        this.initialStock.set(item.geometry, mesh.geometry.attributes.position.array.slice());
      }
      mesh.visible = this.visibility.get(item.component);
      this.bodies.get(item.body).add(mesh);
      this.objects.push(mesh);
    }
    this.pathRoot = new THREE.Group();
    this.bodies.get(manifest.workpiece_body).add(this.pathRoot);
    this.cloudRoot = new THREE.Group();
    this.bodies.get(manifest.workpiece_body).add(this.cloudRoot);
    this.cloudRoot.visible = false;
    return manifest;
  }

  state(state, immediate = false) {
    if (!state || !this.manifest) return;
    this.latest = state;
    state.bodies.forEach((values, id) => {
      const body = this.bodies.get(id);
      if (!body) return;
      body.userData.targetPosition = new THREE.Vector3(...values.slice(0, 3));
      body.userData.targetQuaternion = new THREE.Quaternion(values[4], values[5], values[6], values[3]);
      if (immediate) { body.position.copy(body.userData.targetPosition); body.quaternion.copy(body.userData.targetQuaternion); }
    });
    for (const update of state.mesh_updates || []) {
      const geometry = this.geometries.get(update.id);
      if (!geometry || geometry.attributes.position.count !== update.positions.length) continue;
      geometry.attributes.position.array.set(update.positions.flat());
      geometry.attributes.position.needsUpdate = true;
      geometry.computeVertexNormals();
      geometry.computeBoundingSphere();
    }
  }

  resetStock() {
    for (const [id, positions] of this.initialStock) {
      const geometry = this.geometries.get(id);
      geometry.attributes.position.array.set(positions);
      geometry.attributes.position.needsUpdate = true;
      geometry.computeVertexNormals();
      geometry.computeBoundingSphere();
    }
  }

  paths(paths, selected) {
    if (!this.pathRoot) return;
    for (const child of [...this.pathRoot.children]) { child.geometry.dispose(); child.material.dispose(); }
    this.pathRoot.clear();
    for (const path of paths) {
      const normal = path.normal || [0, 0, 1];
      const points = path.xyz.map((point) => new THREE.Vector3(...point).addScaledVector(new THREE.Vector3(...normal), 0.0006));
      const geometry = new THREE.BufferGeometry().setFromPoints(points);
      const chosen = !selected || selected.has(path.detection_id);
      this.pathRoot.add(new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: chosen ? 0xdd3e76 : 0x99a6ae, depthTest: false, transparent: !chosen, opacity: chosen ? 1 : 0.45 })));
    }
  }

  cloud(cloud) {
    if (!this.cloudRoot) return;
    for (const child of this.cloudRoot.children) { child.geometry.dispose(); child.material.dispose(); }
    this.cloudRoot.clear();
    if (!cloud.points?.length || cloud.frame !== "workpiece_097") return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(cloud.points.flat(), 3));
    const material = new THREE.PointsMaterial({ color: 0x145c70, size: 0.0012, transparent: true, opacity: 0.65, depthTest: false });
    this.cloudRoot.add(new THREE.Points(geometry, material));
  }

  motion(route) {
    for (const child of this.motionRoot.children) { child.geometry.dispose(); child.material.dispose(); }
    this.motionRoot.clear();
    if (route.frame !== "world") return;
    for (const phase of ["transfer", "return", "approach", "trace"]) {
      let points = [];
      const draw = () => {
        if (points.length > 1) this.motionRoot.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color: phase === "trace" ? 0xd36c35 : 0x158d99, transparent: true, opacity: 0.6 })));
        points = [];
      };
      for (const point of route.points || []) {
        if (point.phase === phase) points.push(new THREE.Vector3(...point.xyz)); else draw();
      }
      draw();
    }
  }

  showCloud(value) { if (this.cloudRoot) this.cloudRoot.visible = value; }
  showMotion(value) { if (this.motionRoot) this.motionRoot.visible = value; }

  select(id) {
    this.selected = this.selected === id ? null : id;
    for (const object of this.objects) {
      object.material.emissive.set(object.userData.component === this.selected ? 0x275647 : 0x000000);
      object.material.emissiveIntensity = 0.28;
    }
    this.selectionBox.visible = !!this.selected;
    this.onSelect(this.selected);
  }

  visible(id, value) {
    this.visibility.set(id, value);
    for (const object of this.objects) if (object.userData.component === id) object.visible = value;
    if (id === "workpiece" && this.pathRoot) this.pathRoot.visible = value;
    if (id === "workpiece" && this.cloudRoot) this.cloudRoot.visible = value && document.getElementById("show-cloud").checked;
  }

  colors(value) {
    this.semantic = value;
    for (const object of this.objects) {
      const part = this.components.find((item) => item.id === object.userData.component);
      object.material.color.set(value ? part.color : object.userData.natural);
    }
  }

  view(name) {
    this.follow = name === "workpiece";
    let target, position;
    if (name === "loading") { target = new THREE.Vector3(-0.4, -1.8, 1); position = new THREE.Vector3(0.8, -3.1, 2.1); }
    else if (name === "process") { target = new THREE.Vector3(...(this.manifest?.tool?.contact_world_m || [-1.26, 0, 1.117])); position = target.clone().add(new THREE.Vector3(0.9, 1.3, 0.8)); }
    else if (name === "workpiece" && this.manifest) { target = this.bodies.get(this.manifest.workpiece_body).position.clone().add(new THREE.Vector3(0, 0, 0.22)); position = target.clone().add(new THREE.Vector3(0.38, 0.28, 0.52)); }
    else { target = new THREE.Vector3(-0.7, -0.45, 1.1); position = new THREE.Vector3(3.3, -4.4, 3.5); }
    this.controls.target.copy(target);
    this.camera.position.copy(position);
    this.controls.update();
  }

  orbit(angle, elevation = 0) {
    const offset = this.camera.position.clone().sub(this.controls.target);
    offset.applyAxisAngle(new THREE.Vector3(0, 0, 1), angle);
    if (elevation) offset.z += offset.length() * elevation;
    this.camera.position.copy(this.controls.target).add(offset);
    this.controls.update();
  }

  zoom(amount) { this.camera.position.sub(this.controls.target).multiplyScalar(amount).add(this.controls.target); this.controls.update(); }

  pick(event) {
    if (!this.pointerStart || Math.hypot(event.clientX - this.pointerStart[0], event.clientY - this.pointerStart[1]) > 5) return;
    const bounds = this.renderer.domElement.getBoundingClientRect();
    this.raycaster.setFromCamera(new THREE.Vector2((event.clientX - bounds.left) / bounds.width * 2 - 1, -(event.clientY - bounds.top) / bounds.height * 2 + 1), this.camera);
    const hits = this.raycaster.intersectObjects(this.objects.filter((object) => object.visible));
    if (hits.length) this.select(hits[0].object.userData.component);
  }

  animate() {
    requestAnimationFrame(() => this.animate());
    const now = performance.now(), amount = Math.min(1, (now - this.lastTime) / 90);
    this.lastTime = now;
    for (const body of this.bodies.values()) {
      if (body.userData.targetPosition) { body.position.lerp(body.userData.targetPosition, amount); body.quaternion.slerp(body.userData.targetQuaternion, amount); }
    }
    if (this.follow && this.manifest) {
      const target = this.bodies.get(this.manifest.workpiece_body).position.clone().add(new THREE.Vector3(0, 0, 0.22));
      this.camera.position.add(target.clone().sub(this.controls.target));
      this.controls.target.copy(target);
    }
    this.controls.update();
    this.root.updateMatrixWorld();
    if (this.selected) {
      this.selectionBox.box.makeEmpty();
      for (const object of this.objects) if (object.visible && object.userData.component === this.selected) this.selectionBox.box.expandByObject(object);
    }
    this.renderer.render(this.scene, this.camera);
  }
}
