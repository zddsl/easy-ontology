import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.Statement;

/**
 * 通用一次性 SQL 执行器（诊断用）：输出 TSV，最多 50 行 + 耗时。
 * 用法：java Sql <url> <user> <pass> <driverClass> <queryTimeoutSeconds> "<sql>"
 * 用途：查 DM 会话（V$SESSIONS）、跑对照 SQL 计时。
 */
public class Sql {
    public static void main(String[] args) throws Exception {
        DriverManager.setLoginTimeout(8);
        Class.forName(args[3]);
        long t0 = System.currentTimeMillis();
        int qTimeout = Integer.parseInt(args[4]);
        String sql = args[5];
        if (sql.startsWith("@")) {
            sql = new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(sql.substring(1))), "UTF-8");
        }
        try (Connection c = DriverManager.getConnection(args[0], args[1], args[2]);
             Statement s = c.createStatement()) {
            s.setQueryTimeout(qTimeout);
            boolean hasResult = s.execute(sql);
            if (!hasResult) {
                System.out.println("(update ok) ms=" + (System.currentTimeMillis() - t0));
                return;
            }
            try (ResultSet r = s.getResultSet()) {
                ResultSetMetaData m = r.getMetaData();
                StringBuilder head = new StringBuilder();
                for (int i = 1; i <= m.getColumnCount(); i++) {
                    if (i > 1) head.append('\t');
                    head.append(m.getColumnLabel(i));
                }
                System.out.println(head);
                int rows = 0;
                while (r.next() && rows < 50) {
                    StringBuilder line = new StringBuilder();
                    for (int i = 1; i <= m.getColumnCount(); i++) {
                        if (i > 1) line.append('\t');
                        line.append(r.getString(i));
                    }
                    System.out.println(line);
                    rows++;
                }
                System.out.println("-- shown=" + rows + " ms=" + (System.currentTimeMillis() - t0));
            }
        }
    }
}
