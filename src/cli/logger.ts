import chalk from 'chalk';
export const logger = {
  debug: (tag: string, msg: any) => {
    console.debug(chalk.blue(`[DEBUG: ${tag}]`), msg);
  },

  info: (tag: string, msg: any) => {
    console.info(chalk.green(`[INFO: ${tag}]`), msg);
  },
  warn: (tag: string, msg: any) => {
    console.warn(chalk.yellow(`[WARN: ${tag}]`), msg);
  },
  error: (tag: string, msg: any) => {
    console.warn(chalk.red(`[ERROR: ${tag}]`), msg);
  },
};
