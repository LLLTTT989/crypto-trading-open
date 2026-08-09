# Aqua 三笔交易的事件级核验

核验对象：`0xE01ecFF2F6c4F2416E83e6861e8ABf79b1C95950`。

采用 1inch SwapVM 官方 `Swapped(bytes32,address,address,address,address,uint256,uint256)` 事件，以及 Aqua 官方 `Pulled` / `Pushed` 事件，对以下三笔 Ethereum 交易 receipt 重新解码：

- `0xf6c7e430b05ef3a37ad48cd964e41b5f320d5b40e35c8a471654e6a4ca5c8dee`
- `0x5c03bb9c98ff30bbdfd08c2fa5231b3f747ac1dbfe5e5d71a93e2580caed4da6`
- `0xfb2c0519bdca5394e06d9b6b802a97930f9151df6b91c5c9977b42f7bdc0149d`

## 共同结果

三笔交易均由 SwapVM Router `0x111111338c5091E8440b67B168bAe16a668AC0De` 发出同一条 Aqua 策略的 `Swapped` 事件：

- `orderHash / strategyHash`: `0x172da4dad0c2b873605fcc1cf4bd0072c7f8b7b2fd69e7a50af5d0ad4fd07b99`
- `maker`: `0xE01ecFF2F6c4F2416E83e6861e8ABf79b1C95950`
- `taker`: `0x111116053F09d34a7Eae8102887004445176CA11`
- `tokenIn`: wstETH `0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0`
- `tokenOut`: 1INCH `0x111111111117dc0aa78b770fa6a738034120c302`
- 每次 `amountIn`: `1.000000000000000000 wstETH`
- `tokenIn != tokenOut`，不存在同币种原额 swap。

按 SwapVM 定义，taker 支付 `tokenIn`，maker 提供 `tokenOut`。因此 E01e 在每次成交中是收到 1 wstETH、卖出约 28,286 枚 1INCH，而不是把 1 wstETH 转出去再原额收回。

## 三笔成交明细

| 交易 | Aqua maker 收到 | Aqua maker 卖出 | 隐含价格 |
|---|---:|---:|---:|
| `f6c7...c8dee` | 1 wstETH | 28,286.894995217410142461 1INCH | 28,286.894995 1INCH/wstETH |
| `5c03...d4da6` | 1 wstETH | 28,285.979448007599903161 1INCH | 28,285.979448 1INCH/wstETH |
| `fb2c...c0149d` | 1 wstETH | 28,285.063945879413167782 1INCH | 28,285.063946 1INCH/wstETH |

最高与最低价格相差约 0.6473 bps，符合相同自动化 LP 策略在短时间内连续被填单的表现，但单凭重复性不能证明自成交。

## Aqua 会计事件

每笔都包含：

1. 对同一策略 `Pushed` 1 wstETH；
2. 对同一策略 `Pulled` 对应的约 28,286 1INCH；
3. 另有 `Pulled` 0.0000025 wstETH 到费用接收方。

0.0000025 wstETH 是相对于 1 wstETH 输入的 0.025 bps 协议费。截图把协议的 push/pull 会计转账当成“1 wstETH 出去又回来”，由此推导无损往返，是错误解读。

## 同一交易内的其他 maker

每笔 resolver 交易还同时执行了多条其他 Aqua `Swapped`：其他 maker 用 1INCH 换取 WETH、stETH 或 cbETH。三个 receipt 中出现的其他 maker 包括：

- `0x6fac8047e9d043025484eb0546eb10c5683d3989`
- `0xd6c04e5409523ec94086bd91dd6d2108b3681ebf`
- `0x3e716f13cd4ec7bfc39917f3416a94bf5eec11b5`
- `0x3bb5c8a00190da68059f0f66c24794584eb10d07`
- `0x8047f434c661caf72d60bafd069ea13c87e2f703`
- `0xfff80decd0d72e544caa334fb78c0f5ab1d139f9`

这表明顶层 resolver 交易是在聚合多条 LP 流动性，并非链上直接显示为 E01e 与自己成交。

## 能与不能确定的结论

可以确定：

- 三笔都是 E01e 同一 Aqua 1INCH/wstETH 策略的真实异币种成交；
- 不是同一枚 1 wstETH 原额往返制造 volume；
- 推文截图所依赖的“无损往返”证据不成立；
- 顶层 `tx.from` 是 resolver worker，SwapVM 事件中的 `taker` 是结算/路由合约，不是 E01e。

不能从这三笔确定：

- 最初提交 Fusion intent 的最终用户或签名钱包是否由 E01e 控制；
- E01e 是否在其他钱包、CEX 或其他协议进行了反向对冲；
- 1inch/Merkl 是否将这些 fill 计入或事后剔除奖励。

所以，针对这三笔，确定性定性应为：**正常形式的 Aqua 异币种 fill，存在高频重复但没有链上证据证明 self-trading；推文以 ERC-20 Transfer 截图证明“无损刷量”的论证是误判。**
