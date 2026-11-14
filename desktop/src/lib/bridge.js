// 统一数据桥：Electron 原生 IPC 优先；纯浏览器（vite dev / 截图验收）自动降级为内置虚拟样例。
// 约束：原生模式下既有四个核验通道签名不变；新增三个混币通道按《接口契约_混币法币识别_v0_1.md》第 4 节。
import demoVerifyInput from '../../离线原型/样例/综合模拟样例.json';
import demoVerifyResult from '../../离线原型/样例/核验结果示例.json';
import demoMixerDataset from '../../离线原型/样例/混币出口虚拟样例.json';
import demoMixerResult from '../../离线原型/样例/混币出口识别结果.json';

export const isNative = () => typeof window !== 'undefined' && !!window.chaintrace;
export const DEMO_VERIFY = 'demo://综合模拟样例.json';
export const DEMO_MIXER = 'demo://混币出口虚拟样例.json';

// 时间轴需要 "+00:00 / 6 位微秒" 的规范形态；浏览器演示里由输入自身推导，与引擎口径一致。
const canonical = (value) => {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().slice(0, 19) + '.000000+00:00';
};
const demoTimes = demoVerifyInput.events.map((e) => canonical(e.event_time));

export const bridge = {
  // —— 核验（既有）——
  async chooseInput() {
    if (isNative()) return window.chaintrace.chooseInput();
    throw new Error('浏览器演示模式不支持选择本地文件，请用「内置样例」。');
  },
  async sample() {
    if (isNative()) return window.chaintrace.sample();
    return DEMO_VERIFY;
  },
  async run(file) {
    if (isNative()) return window.chaintrace.run(file);
    if (!file) throw new Error('请先选择模拟输入');
    return { result: demoVerifyResult, input: demoVerifyInput, timelineTimes: demoTimes };
  },
  async export() {
    if (isNative()) return window.chaintrace.export();
    throw new Error('浏览器演示模式不支持导出，请在桌面原型中操作。');
  },

  // —— 混币—法币出口识别（新增）——
  async mixerSample() {
    if (isNative() && window.chaintrace.mixerSample) return window.chaintrace.mixerSample();
    return DEMO_MIXER;
  },
  async runMixer(file) {
    if (isNative() && window.chaintrace.runMixer) {
      // 必须用 mixerSample() 返回的真实路径：主进程只接受自己上次给出的那个路径。
      // 传 DEMO_MIXER（'demo://…'）会被主进程判为"未载入样例"。
      const target = file || await window.chaintrace.mixerSample();
      return window.chaintrace.runMixer(target);
    }
    // 浏览器演示：返回与契约完全一致的静态结果，页面行为与原生一致。
    return { result: demoMixerResult, dataset: demoMixerDataset };
  },
  async exportMixer() {
    if (isNative() && window.chaintrace.exportMixer) return window.chaintrace.exportMixer();
    throw new Error('浏览器演示模式不支持导出，请在桌面原型中操作。');
  },
};

export const demoMixerPayload = () => ({ result: demoMixerResult, dataset: demoMixerDataset });
