import java.io.BufferedOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.Statement;
import java.sql.Types;

/**
 * 流式 JDBC 管道（ABox 生成器专用）：执行 SQL，把结果行吐到 stdout，无行数上限。
 * 用法：java SqlStream <url> <user> <pass> <driverClass> <sqlFile>
 *
 * 管道协议（供 Python 侧 abox_gen.py 消费）：
 *   - 首行：列名，字段以 \x01 分隔
 *   - 数据行：字段以 \x01 分隔；SQL NULL 输出为单独的 \x00 标记
 *   - 字段内转义：'\\'→"\\\\"、'\n'→"\\n"、'\r'→"\\r"、'\x01'→"\\e"
 *   - 出错：错误写 stderr，退出码 1；成功结束时统计写 stderr（rows / ms）
 *   - 输出 UTF-8；每 2000 行 flush 一次（进度及时）
 */
public class SqlStream {
    private static final char SEP = '\u0001';   // 字段分隔
    private static final String NULL_MARK = "\u0000"; // NULL 标记

    public static void main(String[] args) {
        int exit = 0;
        try {
            if (args.length < 5) {
                throw new IllegalArgumentException("usage: SqlStream <url> <user> <pass> <driverClass> <sqlFile>");
            }
            String sql = new String(java.nio.file.Files.readAllBytes(
                    java.nio.file.Paths.get(args[4])), StandardCharsets.UTF_8);
            DriverManager.setLoginTimeout(15);
            Class.forName(args[3]);

            // 显式 UTF-8 + 手动 flush：System.out 平台默认编码不可靠，autoflush 每行太频
            PrintStream out = new PrintStream(new BufferedOutputStream(
                    new FileOutputStream(FileDescriptor.out), 1 << 16), false, "UTF-8");
            long t0 = System.currentTimeMillis();
            long rows = 0;

            try (Connection c = DriverManager.getConnection(args[0], args[1], args[2]);
                 Statement s = c.createStatement(ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
                // 流式取数：MySQL Connector/J 的契约是 fetchSize=Integer.MIN_VALUE
                // （正值会被静默忽略→整表进内存），其余驱动（DM8 等）用 2000 批次提示
                if (args[3].contains("mysql")) s.setFetchSize(Integer.MIN_VALUE);
                else s.setFetchSize(2000);
                try (ResultSet r = s.executeQuery(sql)) {
                    ResultSetMetaData m = r.getMetaData();
                    int n = m.getColumnCount();
                    int[] types = new int[n + 1];
                    StringBuilder head = new StringBuilder();
                    for (int i = 1; i <= n; i++) {
                        types[i] = m.getColumnType(i);
                        if (i > 1) head.append(SEP);
                        head.append(m.getColumnLabel(i));
                    }
                    out.print(head);
                    out.print('\n');

                    StringBuilder line = new StringBuilder(256);
                    while (r.next()) {
                        line.setLength(0);
                        for (int i = 1; i <= n; i++) {
                            if (i > 1) line.append(SEP);
                            String v = columnValue(r, i, types[i]);
                            if (v == null) line.append(NULL_MARK);
                            else appendEncoded(line, v);
                        }
                        line.append('\n');
                        out.print(line);
                        rows++;
                        if (rows % 2000 == 0) out.flush();
                    }
                    out.flush();
                }
            }
            System.err.println("SqlStream ok rows=" + rows + " ms=" + (System.currentTimeMillis() - t0));
        } catch (Exception e) {
            exit = 1;
            System.err.println("SqlStream error: " + e);
            e.printStackTrace();
        }
        System.exit(exit);
    }

    /** 按列 JDBC 类型取标准字符串（日期/时间用规范格式，其余 getString）。NULL 返回 null。 */
    private static String columnValue(java.sql.ResultSet r, int i, int type) throws java.sql.SQLException {
        switch (type) {
            case Types.DATE: {
                java.sql.Date d = r.getDate(i);
                return d == null ? null : d.toString(); // yyyy-MM-dd
            }
            case Types.TIME: {
                java.sql.Time t = r.getTime(i);
                return t == null ? null : t.toString(); // HH:mm:ss
            }
            case Types.TIMESTAMP: {
                java.sql.Timestamp ts = r.getTimestamp(i);
                return ts == null ? null : ts.toString().replace(' ', 'T'); // xsd:dateTime 的 T 分隔
            }
            default:
                return r.getString(i);
        }
    }

    /** 字段内转义（防破坏行协议）：'\\'→"\\\\"、'\n'→"\\n"、'\r'→"\\r"、'\u0001'→"\\e"。 */
    private static void appendEncoded(StringBuilder sb, String v) {
        for (int i = 0; i < v.length(); i++) {
            char ch = v.charAt(i);
            if (ch == '\\') sb.append("\\\\");
            else if (ch == '\n') sb.append("\\n");
            else if (ch == '\r') sb.append("\\r");
            else if (ch == SEP) sb.append("\\e");
            else sb.append(ch);
        }
    }
}
