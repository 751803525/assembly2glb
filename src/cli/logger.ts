import chalk from 'chalk';
export const logger = {
  debug: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.debug(chalk.blue(`[${tag}]`), message, ...optionalParams);
  },
  info: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.info(chalk.green(`[${tag}]`), message, ...optionalParams);
  },
  warn: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.warn(chalk.yellow(`[${tag}]`), message, ...optionalParams);
  },

  error: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.error(chalk.red(`[${tag}]`), message, ...optionalParams);
  },
};
