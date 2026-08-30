# RT-503 复盘（fixture 数据，非真实 RT）

存在的唯一目的：让本 fixture RT 成为一个**正常关闭**的 RT（status=done 且过 G111），
从而使 `g112-done-scope-exempt` 用例断言的是 G112 的作用域豁免本身，
而不是被 G111 的硬失败掩盖。内容不参与任何判据。
